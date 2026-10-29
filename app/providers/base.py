import asyncio
import json
import re
import time
from html import unescape

import httpx

from ..config import CACHE_TTL_SECONDS, HTTP_TIMEOUT, MAX_CONCURRENCY, USER_AGENT
from ..db import SessionLocal
from ..geo import haversine_km
from ..models import HttpCache
from ..parser import normalize
from ..schemas import Filtros, FonteStatus, Imovel


class FetchError(Exception):
    def __init__(self, status: int, url: str):
        super().__init__(f"HTTP {status} em {url}")
        self.status = status


class Fetcher:
    """Cliente HTTP com cache em SQLite e limite de requisições simultâneas."""

    def __init__(self):
        self._sem = asyncio.Semaphore(MAX_CONCURRENCY)
        self._client = httpx.AsyncClient(
            timeout=HTTP_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "pt-BR,pt;q=0.9"},
        )

    async def get(self, url: str, use_cache: bool = True) -> str:
        if use_cache:
            with SessionLocal() as db:
                row = db.get(HttpCache, url)
                if row and time.time() - row.fetched_at < CACHE_TTL_SECONDS:
                    return row.body
        async with self._sem:
            resp = await self._client.get(url)
        if resp.status_code != 200:
            raise FetchError(resp.status_code, url)
        with SessionLocal() as db:
            db.merge(HttpCache(url=url, body=resp.text, fetched_at=time.time()))
            db.commit()
        return resp.text

    async def aclose(self):
        await self._client.aclose()


# Bairro de referência: {"bairro", "slug", "regiao", "regiao_slug", "latitude", "longitude"}
Referencia = dict


class Provider:
    """Uma fonte de anúncios.

    `search` recebe os filtros já interpretados e o bairro de referência (ou None) e devolve os
    imóveis com `tipo` normalizado (apartamento, casa, cobertura, area-privativa, kitnet),
    `distancia_km` preenchida quando houver referência, e o status da fonte.
    """

    name = "base"

    @property
    def label(self) -> str:
        """Nome para exibição; cada provedor pode sobrescrever com `label = "..."`."""
        return self.name.capitalize()

    async def search(self, filtros: Filtros, fetcher: Fetcher, max_detalhes: int,
                     ref: Referencia | None) -> tuple[list[Imovel], FonteStatus]:
        raise NotImplementedError


def passa_basico(im: Imovel, f: Filtros) -> bool:
    """Filtros que dão para checar só com os dados da listagem; campo desconhecido não elimina."""
    if f.quartos_min and im.quartos is not None and im.quartos < f.quartos_min:
        return False
    if f.quartos_max and im.quartos is not None and im.quartos > f.quartos_max:
        return False
    if f.preco_max and im.aluguel is not None and im.aluguel > f.preco_max:
        return False
    if f.total_max and im.aluguel is not None and im.aluguel + (im.condominio or 0) > f.total_max:
        return False
    if f.preco_min and im.aluguel is not None and im.aluguel < f.preco_min:
        return False
    if f.vagas_min and im.vagas is not None and im.vagas < f.vagas_min:
        return False
    return True


def aplica_distancia(imoveis: list[Imovel], ref: Referencia | None, f: Filtros) -> list[Imovel]:
    """Calcula distancia_km e, se houver raio, descarta quem está fora ou não tem coordenadas."""
    if not ref:
        return imoveis
    out = []
    for im in imoveis:
        if im.latitude is not None and im.longitude is not None:
            im.distancia_km = round(haversine_km(ref["latitude"], ref["longitude"], im.latitude, im.longitude), 2)
        if f.raio_km and (im.distancia_km is None or im.distancia_km > f.raio_km):
            continue
        out.append(im)
    return out


def json_ld_blocks(html: str) -> list:
    out = []
    for m in re.finditer(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S):
        try:
            out.append(json.loads(m.group(1)))
        except json.JSONDecodeError:
            pass
    return out


def strip_tags(html: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html))).strip()


def money(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"(\d[\d.]*(?:,\d+)?)", text)
    if not m:
        return None
    return float(m.group(1).replace(".", "").replace(",", "."))


# --- Inferência de pet e garantias a partir do texto do anúncio ---------------------------

_NEG = r"(?:nao|proibid\w*|vedad\w*|sem)\s+(?:se\s+)?(?:aceit\w*|permit\w*)?\s*"
_PET = r"(?:animais|animal|pets?|cachorros?|caes|gatos?|bichos?)"
_GARANTIAS = {
    "seguro_fianca": r"seguro[\s-]*fianca",
    "fiador": r"fiador",
    "caucao": r"caucao|deposito\s+(?:caucao|de\s+garantia)",
    "titulo_capitalizacao": r"titulo\s+de\s+capitaliza\w*|capitalizacao",
}


def infer_pet(texto: str) -> bool | None:
    t = normalize(texto)
    if re.search(rf"{_NEG}{_PET}|{_PET}\s+(?:nao\s+(?:sao\s+)?(?:aceit|permit)\w*|proibid\w*)|restricao\s+(?:para|a)\s+{_PET}", t):
        return False
    if re.search(rf"(?:aceit\w*|permit\w*|pode\w*\s+ter|liberad\w*)\s+(?:de\s+)?{_PET}|pet\s*friendly|{_PET}\s+(?:sao\s+)?(?:bem[\s-]vindos|permitid\w*|aceit\w*)", t):
        return True
    return None


_ORDINAIS = {"primeiro": 1, "segundo": 2, "terceiro": 3, "quarto": 4, "quinto": 5}


def infer_andar(texto: str) -> tuple[int | None, bool | None]:
    """Andar do imóvel segundo o anúncio (0 = térreo) e se ele se diz 'andar alto'."""
    t = normalize(texto)
    alto = True if re.search(r"\bandar\s+alto\b", t) else None
    # "situado no 3º andar"; ignora "2 apartamentos por andar" e "19 andares"
    if m := re.search(r"\b(\d{1,2})\s*(?:o|a|deg)?\s*andar\b", t):
        return int(m.group(1)), alto
    if m := re.search(r"\b(primeiro|segundo|terceiro|quarto|quinto)\s+andar\b", t):
        return _ORDINAIS[m.group(1)], alto
    if re.search(r"\b(?:apartamento|apto|unidade|imovel)\s+(?:no\s+|e\s+)?terreo\b|\bandar\s+terreo\b", t):
        return 0, alto
    return None, alto


def infer_garantias(texto: str) -> dict[str, bool | None]:
    # "sem fiador", "dispensa caução" é propaganda de "não precisa", não diz se a garantia é aceita
    t = re.sub(r"\b(?:sem|dispensa\w*|livre de)\s+(?:de\s+)?(?:fiador\w*|caucao|deposito)", " ", normalize(texto))
    res: dict[str, bool | None] = {k: None for k in _GARANTIAS}
    for key, rx in _GARANTIAS.items():
        if re.search(rf"nao\s+(?:aceit\w*|trabalh\w*\s+com|e\s+aceit\w*)\s+(?:\w+\s+){{0,3}}?(?:{rx})", t):
            res[key] = False
        elif re.search(rx, t):
            res[key] = True
    # "somente/apenas seguro fiança" => as demais não são aceitas
    for key, rx in _GARANTIAS.items():
        if re.search(rf"(?:somente|apenas|exclusivamente|so)\s+(?:com\s+)?(?:{rx})", t):
            for other in res:
                if other != key and res[other] is None:
                    res[other] = False
    # Anúncio que lista as garantias aceitas: o que não está na lista não é aceito
    if re.search(r"garantias?\b", t) and any(res.values()):
        res = {k: (False if v is None else v) for k, v in res.items()}
    return res
