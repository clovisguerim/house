import asyncio
import json
import re
from html import unescape

from ..parser import normalize
from ..schemas import Filtros, FonteStatus, Imovel
from .base import (FetchError, Fetcher, Provider, Referencia, aplica_distancia, infer_andar, infer_garantias,
                   infer_pet, money, passa_basico)

BASE = "https://www.lopes.com.br"
CIDADE = "busca/aluguel/br/mg/belo-horizonte"
# Tipos de imóvel com página própria na Lopes; área privativa e kitnet não existem lá
TIPO_SLUG = {"apartamento": "apartamento", "casa": "casa", "cobertura": "cobertura"}
TIPO_NORMAL = {
    "apartamento": "apartamento", "flat": "kitnet", "kitnet": "kitnet", "studio": "kitnet", "loft": "kitnet",
    "casa": "casa", "casa de condominio": "casa", "sobrado": "casa", "cobertura": "cobertura",
}


def _estado(html: str) -> list[dict]:
    """Anúncios do estado que o Angular serializa na página (script#ng-state)."""
    m = re.search(r'<script id="ng-state" type="application/json">(.*?)</script>', html, re.S)
    if not m:
        return []
    try:
        state = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []
    out = []
    for key, value in state.items():
        if key.startswith("search/") and isinstance(value, dict):
            out.extend((value.get("products") or {}).get("content") or [])
    return out


def _atributo(p: dict, tipo: str) -> str | None:
    return next((a.get("value") for a in p.get("attributes") or [] if a.get("type") == tipo), None)


def _num(v: str | None) -> float | None:
    m = re.search(r"\d+(?:[.,]\d+)?", v or "")
    return float(m.group(0).replace(",", ".")) if m else None


def _imovel(p: dict) -> Imovel | None:
    loc = p.get("locationDTO") or {}
    # A busca por cidade às vezes mistura anúncios de outras cidades (ex.: São Paulo)
    if p.get("dealType") != "rent" or normalize(loc.get("city") or "") != "belo horizonte":
        return None
    tipo = TIPO_NORMAL.get(normalize(p.get("divisionUnitType") or p.get("type") or ""))
    if not tipo:
        return None  # loja, sala, galpão...

    precos = unescape(p.get("subPrice") or "")

    def preco(rotulo: str) -> float | None:
        m = re.search(rf"{rotulo}:\s*R\$\s*([\d.,]+)", precos)
        return money(m.group(1)) if m else None

    quartos, banheiros, vagas = (_num(_atributo(p, t)) for t in ("bedroom_attr", "bathroom_attr", "parking_lots_attr"))
    bairro = loc.get("neighborhood") or (p.get("neighborhood") or "").split(",")[0] or None
    descricao = unescape(p.get("description") or "")
    andar, andar_alto = infer_andar(descricao)
    g = infer_garantias(descricao)
    geo = p.get("geolocation") or {}
    return Imovel(
        fonte="lopes",
        id=p["id"],
        url=f"{BASE}/imovel/{p['id']}",
        titulo=(p.get("analytics") or {}).get("name") or p.get("imageAlternateText"),
        tipo=tipo,
        endereco=", ".join(x for x in (p.get("street"), bairro, "Belo Horizonte - MG") if x),
        bairro=bairro,
        latitude=geo.get("lat", p.get("lat")),
        longitude=geo.get("lng", p.get("lng")),
        quartos=int(quartos) if quartos is not None else None,
        banheiros=int(banheiros) if banheiros is not None else None,
        area_m2=_num(_atributo(p, "area_attr")),
        aluguel=preco("Aluguel"),
        condominio=preco("Condom[ií]nio"),
        iptu=preco("IPTU"),
        vagas=int(vagas) if vagas is not None else None,
        andar=andar,
        andar_alto=andar_alto,
        aceita_pet=infer_pet(descricao),
        descricao=descricao or None,
        seguro_fianca=g["seguro_fianca"],
        fiador=g["fiador"],
        caucao=g["caucao"],
        titulo_capitalizacao=g["titulo_capitalizacao"],
        fotos=[f["largeUrl"] for f in p.get("photo") or [] if (f.get("largeUrl") or "").startswith("https://")],
    )


class LopesProvider(Provider):
    """Lopes: a página de busca é Angular com SSR e traz os anúncios (com coordenadas, preços e
    descrição completa) no estado serializado, então não é preciso abrir cada anúncio.

    O estoque de locação residencial da Lopes em BH é pequeno; a busca por bairro usa um raio fixo
    do próprio site, então consultamos a cidade inteira por tipo e filtramos a distância aqui.
    """

    name = "lopes"
    label = "Lopes"

    async def search(self, filtros: Filtros, fetcher: Fetcher, max_detalhes: int, ref: Referencia | None):
        tipos = [TIPO_SLUG[t] for t in filtros.tipos if t in TIPO_SLUG]
        # Área privativa/kitnet não têm página própria: vêm misturados nas demais, se houver
        if not tipos or any(t not in TIPO_SLUG for t in filtros.tipos):
            tipos = sorted(set(tipos) | {"apartamento"})
        urls = [f"{BASE}/{CIDADE}/tipo/{t}" for t in tipos]

        erros = []

        async def listar(url: str) -> list[dict]:
            try:
                return _estado(await fetcher.get(url))
            except FetchError as e:
                erros.append(e.status)
                return []

        vistos: dict[str, Imovel] = {}
        for lote in await asyncio.gather(*(listar(u) for u in urls)):
            for p in lote:
                if p.get("id") and p["id"] not in vistos and (im := _imovel(p)):
                    vistos[im.id] = im

        if erros and len(erros) == len(urls):
            return [], FonteStatus(fonte=self.name, ok=False, mensagem=f"Lopes respondeu HTTP {erros[0]}")

        candidatos = [im for im in vistos.values() if passa_basico(im, filtros)][:max_detalhes]
        resultado = aplica_distancia(candidatos, ref, filtros)
        msg = f"{len(urls)} páginas de listagem consultadas, {len(vistos)} anúncios residenciais em BH"
        return resultado, FonteStatus(fonte=self.name, ok=True, mensagem=msg, encontrados=len(resultado))
