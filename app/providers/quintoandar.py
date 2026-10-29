import asyncio
import json
import re

from ..parser import slugify
from ..schemas import Filtros, FonteStatus, Imovel
from .base import (FetchError, Fetcher, Provider, Referencia, aplica_distancia, infer_andar, infer_garantias,
                   infer_pet, json_ld_blocks, passa_basico)

BASE = "https://www.quintoandar.com.br"
CIDADE = "belo-horizonte-mg-brasil"
FOTO = f"{BASE}/img/med/"
# robots.txt bloqueia paginação via busca (/busca, ?search_id=…), então cada página rende até 24 anúncios
MAX_PAGINAS = 30

# Segmentos de URL por tipo pedido. QuintoAndar não tem "área privativa": cai em apartamento.
TIPO_SEGMENTOS = {
    "apartamento": ["apartamento"],
    "area-privativa": ["apartamento"],
    "cobertura": ["apartamento/apartamento-cobertura"],
    "casa": ["casa"],
    "kitnet": ["kitnet"],
}
TIPO_QA = {"Apartamento": "apartamento", "Casa": "casa", "CasaCondominio": "casa", "StudioOuKitchenette": "kitnet"}
SEM_GARANTIA = "Sem fiador/caução (garantia QuintoAndar)"


def _estado(html: str) -> dict:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))["props"]["pageProps"].get("initialState") or {}
    except (json.JSONDecodeError, KeyError):
        return {}


def _vizinhos(estado: dict) -> list[str]:
    """Slugs dos bairros vizinhos que o próprio site lista no rodapé da página do bairro."""
    s = json.dumps(estado.get("footer") or estado, ensure_ascii=False)
    out = []
    for m in re.finditer(r'"type": "nearby_neighborhoods", "links": (\[.*?\])', s):
        for link in json.loads(m.group(1)):
            if mm := re.match(rf"/alugar/imovel/([a-z0-9-]+)-{CIDADE}$", link.get("link", "")):
                out.append(mm.group(1))
    return out


def _tipo(tipo_qa: str | None, amenities: list[str]) -> str | None:
    if "APARTAMENTO_COBERTURA" in amenities:
        return "cobertura"
    return TIPO_QA.get(tipo_qa or "", (tipo_qa or "").lower() or None)


def _cards(estado: dict) -> list[Imovel]:
    """Anúncios da listagem (initialState.houses), na ordem em que o site exibe."""
    houses = estado.get("houses") or {}
    ordem = ((estado.get("search") or {}).get("visibleHouses") or {}).get("pages", {}).get("0") or list(houses)
    out = []
    for hid in ordem:
        h = houses.get(hid)
        if not isinstance(h, dict) or not h.get("forRent") or not h.get("rentPrice"):
            continue
        amenities = h.get("amenities") or []
        out.append(Imovel(
            fonte="quintoandar",
            id=str(h["id"]),
            url=f"{BASE}/imovel/{h['id']}/alugar",
            titulo=h.get("shortRentDescription"),
            tipo=_tipo(h.get("type"), amenities),
            endereco=(h.get("address") or {}).get("address"),
            bairro=h.get("neighbourhood") or h.get("regionName"),
            quartos=h.get("bedrooms"),
            banheiros=h.get("bathrooms"),
            area_m2=h.get("area"),
            aluguel=h.get("rentPrice"),
            vagas=h.get("parkingSpots"),
            aceita_pet=True if "PODE_TER_ANIMAIS_DE_ESTIMACAO" in amenities else None,
            fotos=[FOTO + p["url"] for p in (h.get("photos") or []) if p.get("url")][:10],
        ))
    return out


def _detalhes(im: Imovel, html: str) -> bool:
    """Completa o anúncio com a página do imóvel. Devolve False se ele não está mais disponível."""
    info = ((_estado(html).get("house") or {}).get("houseInfo")) or {}
    if not info.get("rentPrice") or info.get("status") not in (None, "publicado"):
        return False
    addr = info.get("address") or {}
    im.latitude, im.longitude = addr.get("lat"), addr.get("lng")
    im.endereco = ", ".join(x for x in (addr.get("street"), addr.get("neighborhood"), addr.get("city")) if x) or im.endereco
    im.bairro = addr.get("neighborhood") or im.bairro
    im.aluguel = info["rentPrice"]
    im.condominio = 0 if info.get("condoType") == "IncluidoNoAluguel" else info.get("condoPrice")
    im.iptu = info.get("iptu")
    im.quartos = info.get("bedrooms", im.quartos)
    im.banheiros = info.get("bathrooms", im.banheiros)
    im.area_m2 = info.get("area", im.area_m2)
    im.vagas = info.get("parkingSpaces", im.vagas)
    if info.get("photos"):
        im.fotos = [FOTO + p["url"] for p in info["photos"] if p.get("url")][:10]
    for block in json_ld_blocks(html):
        for node in (block.get("@graph", []) if isinstance(block, dict) else []):
            if node.get("@type") == "RealEstateListing" and node.get("url"):
                im.url = node["url"]
                im.titulo = node.get("name") or im.titulo

    sim = [a for a in (info.get("amenities") or []) + (info.get("installations") or []) if a.get("value") == "SIM"]
    im.tipo = _tipo(info.get("type"), [a.get("key") for a in sim]) or im.tipo
    im.caracteristicas = [a["text"] for a in sim if a.get("text")]

    texto = " ".join([info.get("remarks") or "",
                      (info.get("generatedDescription") or {}).get("longDescription") or ""])
    im.descricao = texto.strip() or None
    faixa = info.get("rangeFloor") or {}
    andar, im.andar_alto = infer_andar(texto)
    if faixa.get("min") is not None and faixa.get("min") == faixa.get("max"):
        andar = faixa["min"]
    elif faixa.get("max") is not None:
        im.caracteristicas.append(f"Andar entre {faixa.get('min', 0) or 'térreo'} e {faixa['max']}")
    im.andar = andar

    pet = info.get("acceptsPets")
    im.aceita_pet = pet if isinstance(pet, bool) else infer_pet(texto)
    g = infer_garantias(texto)
    im.seguro_fianca, im.fiador, im.caucao, im.titulo_capitalizacao = (
        g["seguro_fianca"], g["fiador"], g["caucao"], g["titulo_capitalizacao"])
    im.caracteristicas.append(SEM_GARANTIA)
    return True


class QuintoAndarProvider(Provider):
    name = "quintoandar"
    label = "QuintoAndar"

    async def search(self, filtros: Filtros, fetcher: Fetcher, max_detalhes: int, ref: Referencia | None):
        segmentos = list(dict.fromkeys(s for t in filtros.tipos for s in TIPO_SEGMENTOS.get(t, [])))
        if not segmentos:
            return [], FonteStatus(fonte=self.name, ok=True, mensagem="Nenhum tipo pedido existe no QuintoAndar")
        quartos = f"/{filtros.quartos_min}-quartos" if filtros.quartos_min else ""

        locais = [""]  # cidade inteira
        if ref:
            # A página do bairro informa os vizinhos; as páginas de bairro já incluem os arredores
            locais = [ref["slug"]]
            vizinhos: list[str] = []
            try:
                vizinhos = _vizinhos(_estado(await fetcher.get(f"{BASE}/alugar/imovel/{ref['slug']}-{CIDADE}")))
            except FetchError:
                pass
            if filtros.raio_km:
                extras = [ref.get("regiao_slug"), *vizinhos]
                if filtros.raio_km >= 6:
                    extras.append("")
                vagas = max(0, MAX_PAGINAS // len(segmentos) - 1)
                locais += [x for x in dict.fromkeys(extras) if x is not None and x != ref["slug"]][:vagas]

        urls = [
            f"{BASE}/alugar/imovel/{loc + '-' if loc else ''}{CIDADE}/{seg}{quartos}"
            for loc in locais for seg in segmentos
        ][:MAX_PAGINAS]

        async def listar(url: str) -> list[Imovel]:
            try:
                return _cards(_estado(await fetcher.get(url)))
            except FetchError:
                return []

        vistos: dict[str, Imovel] = {}
        for lote in await asyncio.gather(*(listar(u) for u in urls)):
            for im in lote:
                vistos.setdefault(im.id, im)
        candidatos = [im for im in vistos.values() if passa_basico(im, filtros)]
        if ref:
            # Bairro de referência primeiro, caso max_detalhes corte a lista
            candidatos.sort(key=lambda im: slugify(im.bairro or "") != ref["slug"])
        candidatos = candidatos[:max_detalhes]

        async def detalhar(im: Imovel) -> Imovel | None:
            try:
                return im if _detalhes(im, await fetcher.get(f"{BASE}/imovel/{im.id}")) else None
            except FetchError:
                return None

        detalhados = [im for im in await asyncio.gather(*(detalhar(im) for im in candidatos)) if im]
        resultado = [im for im in aplica_distancia(detalhados, ref, filtros) if passa_basico(im, filtros)]
        msg = (f"{len(urls)} páginas de listagem consultadas, {len(detalhados)} anúncios detalhados. "
               "QuintoAndar dispensa fiador/caução (garantia própria); não há tipo 'área privativa'")
        return resultado, FonteStatus(fonte=self.name, ok=True, mensagem=msg, encontrados=len(resultado))
