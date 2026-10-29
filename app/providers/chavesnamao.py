import asyncio
import json
import re
from collections import Counter
from html import unescape

from ..geo import haversine_km
from ..parser import normalize, slugify
from ..schemas import Filtros, FonteStatus, Imovel
from .base import (FetchError, Fetcher, Provider, Referencia, aplica_distancia, infer_andar, infer_garantias,
                   infer_pet, json_ld_blocks, money, passa_basico, strip_tags)

BASE = "https://www.chavesnamao.com.br"
CIDADE = "mg-belo-horizonte"
# Área privativa não tem listagem própria: vem de "apartamentos" e é reconhecida pelo texto do anúncio
TIPO_PATHS = {
    "apartamento": ["apartamentos"],
    "area-privativa": ["apartamentos"],
    "cobertura": ["coberturas"],
    "casa": ["casas", "casas-em-condominio"],
    "kitnet": ["kitnet"],
}
# robots.txt bloqueia query string, exceto exatamente "?pg=2": no máximo 2 páginas (30 anúncios) por URL
PAGINAS = (1, 2)
MAX_PAGINAS = 30
MAX_BAIRROS_VIZINHOS = 8


def _rsc(html: str) -> tuple[str, dict[str, str]]:
    """Junta o payload RSC do Next.js e extrai as linhas de texto ("b4:T3e8,...") referenciadas como "$b4"."""
    partes = [json.loads(f'"{m.group(1)}"')
              for m in re.finditer(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', html)]
    rsc = "".join(partes)
    raw = rsc.encode("utf-8")
    textos = {}
    for m in re.finditer(rb"(?:^|\n)([0-9a-f]+):T([0-9a-f]+),", raw):
        n = int(m.group(2), 16)  # tamanho em bytes UTF-8
        textos[m.group(1).decode()] = raw[m.end():m.end() + n].decode("utf-8", "replace")
    return rsc, textos


def _objetos(rsc: str, padrao: str) -> list[dict]:
    dec = json.JSONDecoder()
    out = []
    for m in re.finditer(padrao, rsc):
        try:
            obj, _ = dec.raw_decode(rsc, m.start() + m.group(0).rfind("{"))
        except json.JSONDecodeError:
            continue
        out.append(obj)
    return out


def _texto(valor, textos: dict[str, str]) -> str:
    if isinstance(valor, str) and valor.startswith("$"):
        valor = textos.get(valor[1:], "")
    return strip_tags(unescape(valor or "").replace("<br/>", " "))


def _d(v) -> dict:
    """Campo do payload RSC que deveria ser objeto; referências "$..." viram dict vazio."""
    return v if isinstance(v, dict) else {}


def _l(v) -> list:
    return v if isinstance(v, list) else []


def _float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _tipo(realty: str | None, texto: str = "") -> str | None:
    r = normalize(realty or "")
    if not r:
        return None
    if "cobertura" in r:
        return "cobertura"
    if "casa" in r or "sobrado" in r or "house" in r or "residence" in r:
        return "casa"
    if any(k in r for k in ("kitnet", "flat", "loft", "studio")):
        return "kitnet"
    if "apartamento" in r or "apartment" in r:
        return "area-privativa" if re.search(r"\barea\s+privativa\b", normalize(texto)) else "apartamento"
    return r.replace(" ", "-")


def _endereco(loc: dict) -> str | None:
    rua = _d(loc.get("street")) if loc.get("publicAddress", True) else {}
    partes = [rua.get("name"), rua.get("addressNumber") if rua.get("addressNumber") not in ("", "--") else None,
              _d(loc.get("neighborhood")).get("name") or loc.get("neighborhoodName")]
    partes = [p for p in partes if p]
    return f"{', '.join(partes)}, Belo Horizonte - MG" if partes else None


def _listagem(html: str) -> list[Imovel]:
    """Anúncios de uma página de listagem: geo/endereço do JSON-LD + preços, vagas e área do payload RSC."""
    ld: dict[str, dict] = {}
    for block in json_ld_blocks(html):
        if not isinstance(block, dict) or block.get("@type") != "RealEstateListing":
            continue
        for el in _d(block.get("offers")).get("itemListElement", []):
            if m := re.search(r"/id-(\d+)/", el.get("url", "")):
                ld[m.group(1)] = el

    rsc, _ = _rsc(html)
    dados = {str(o["id"]): o for o in _objetos(rsc, r'"dataBySegment":\{') if "id" in o and "url" in o}

    out = []
    for id_, el in ld.items():
        prop = _d(el.get("itemOffered"))
        geo = _d(prop.get("geo"))
        addr = _d(prop.get("address"))
        d = dados.get(id_, {})
        prices = _d(d.get("prices"))
        loc = _d(d.get("location"))
        area = _float(_d(d.get("area")).get("useful"))
        if area is None and (fs := _d(prop.get("floorSize")).get("unitText")):
            area = money(fs)
        endereco = _endereco(loc) if loc else None
        if not endereco and addr.get("streetAddress"):
            endereco = f"{addr['streetAddress']}, {addr.get('addressLocality', '')}, Belo Horizonte - MG"
        out.append(Imovel(
            fonte="chavesnamao",
            id=id_,
            url=el["url"],
            titulo=d.get("title") or el.get("name"),
            tipo=_tipo(_d(d.get("realtyType")).get("name") or prop.get("@type"), d.get("title") or ""),
            endereco=endereco,
            bairro=_d(loc.get("neighborhood")).get("name") or addr.get("addressLocality"),
            latitude=_float(geo.get("latitude")),
            longitude=_float(geo.get("longitude")),
            quartos=_d(d.get("bedrooms")).get("count", prop.get("numberOfBedrooms")),
            banheiros=_d(d.get("bathrooms")).get("count", prop.get("numberOfBathroomsTotal")),
            area_m2=area,
            aluguel=_float(prices.get("rawPrice")) or _float(el.get("price")),
            condominio=money(prices.get("condominiumFee")),
            iptu=money(prices.get("iptuValue")),
            vagas=_d(d.get("garages")).get("count") if d else None,
            fotos=[prop["image"]] if isinstance(prop.get("image"), str) else [],
        ))
    return out


def _detalhes(im: Imovel, html: str) -> None:
    rsc, textos = _rsc(html)
    objs = [o for o in _objetos(rsc, rf'\{{"id":{im.id},') if "prices" in o]
    if not objs:
        return
    o = max(objs, key=len)
    loc = _d(o.get("location"))
    if geo := _d(loc.get("geoposition")):
        im.latitude, im.longitude = _float(geo.get("lat")), _float(geo.get("lon"))
    if end := _endereco(loc):
        im.endereco = end
    prices = _d(o.get("prices"))
    im.condominio = money(prices.get("condominiumFee")) or im.condominio
    im.iptu = money(prices.get("iptuValue")) or im.iptu
    im.vagas = _d(o.get("garages")).get("count", im.vagas)
    itens = [i["name"] for k in ("commonItems", "privativeItems") for i in _l(o.get(k)) if isinstance(i, dict) and i.get("name")]
    im.caracteristicas = list(dict.fromkeys(itens))
    desc = _texto(o.get("description"), textos) or _texto(o.get("descriptionRaw"), textos)
    texto = " ".join([o.get("title") or "", desc, *im.caracteristicas])
    im.descricao = desc or None
    im.tipo = _tipo(_d(o.get("realtyType")).get("name"), texto) or im.tipo
    im.andar, im.andar_alto = infer_andar(texto)
    # petFriendly vem false até em anúncio com "Permitido pets"; só o true é confiável
    im.aceita_pet = True if o.get("petFriendly") is True else infer_pet(texto)
    g = infer_garantias(texto)
    im.seguro_fianca, im.fiador, im.caucao, im.titulo_capitalizacao = (
        g["seguro_fianca"], g["fiador"], g["caucao"], g["titulo_capitalizacao"])
    if fotos := _imagens(json_ld_blocks(html)):
        im.fotos = fotos[:10]


def _imagens(node) -> list[str]:
    """Primeira lista de URLs em um campo "image" em qualquer nível do JSON-LD."""
    if isinstance(node, dict):
        imgs = node.get("image")
        if isinstance(imgs, list) and imgs and all(isinstance(i, str) and i.startswith("https://") for i in imgs):
            return imgs
        node = list(node.values())
    if isinstance(node, list):
        for child in node:
            if found := _imagens(child):
                return found
    return []


class ChavesNaMaoProvider(Provider):
    name = "chavesnamao"
    label = "Chaves na Mão"

    async def search(self, filtros: Filtros, fetcher: Fetcher, max_detalhes: int, ref: Referencia | None):
        paths = list(dict.fromkeys(p for t in filtros.tipos for p in TIPO_PATHS.get(t, [])))
        q = min(filtros.quartos_min, 4) if filtros.quartos_min else None  # "N-quartos" = N ou mais
        seg_quartos = f"{q}-quarto{'s' if q > 1 else ''}/" if q else ""
        paginas = 0
        bloqueios = 0

        async def listar(path: str, bairro: str | None, pg: int) -> list[Imovel] | None:
            nonlocal paginas, bloqueios
            if paginas >= MAX_PAGINAS:
                return []
            paginas += 1
            url = f"{BASE}/{path}-para-alugar/{CIDADE}/{bairro + '/' if bairro else ''}{seg_quartos}"
            try:
                return _listagem(await fetcher.get(url + ("?pg=2" if pg == 2 else "")))
            except FetchError as e:
                if e.status in (403, 429, 503):
                    bloqueios += 1
                return None  # 404: bairro inexistente no site

        async def cobrir(bairro: str | None, pgs=PAGINAS) -> list[Imovel]:
            lotes = await asyncio.gather(*(listar(p, bairro, pg) for p in paths for pg in pgs))
            return [im for lote in lotes if lote for im in lote]

        vistos: dict[str, Imovel] = {}

        def guardar(lote: list[Imovel]):
            for im in lote:
                vistos.setdefault(im.id, im)

        if ref:
            slug = ref["slug"]
            lote = await cobrir(slug)
            if not lote and slugify(ref["bairro"]) != slug:
                slug = slugify(ref["bairro"])
                lote = await cobrir(slug)
            if not lote:  # bairro sem página própria: usa a cidade e filtra pela distância
                lote = await cobrir(None)
            guardar(lote)

            # A página de um bairro também traz vizinhos; os vizinhos dentro do raio viram novas páginas
            if filtros.raio_km:
                contagem: Counter = Counter()
                dist: dict[str, float] = {}
                for im in vistos.values():
                    if im.bairro and im.latitude is not None:
                        d = haversine_km(ref["latitude"], ref["longitude"], im.latitude, im.longitude)
                        s = slugify(im.bairro)
                        if s != slug and d <= filtros.raio_km:
                            contagem[s] += 1
                            dist[s] = min(dist.get(s, 1e9), d)
                vizinhos = sorted(contagem, key=lambda s: (dist[s], -contagem[s]))[:MAX_BAIRROS_VIZINHOS]
                for lote in await asyncio.gather(*(cobrir(s, (1,)) for s in vizinhos)):
                    guardar(lote)
        else:
            guardar(await cobrir(None))

        if not vistos and bloqueios:
            return [], FonteStatus(fonte=self.name, ok=False,
                                   mensagem="Chaves na Mão bloqueou o acesso automatizado")

        candidatos = [im for im in aplica_distancia(list(vistos.values()), ref, filtros) if passa_basico(im, filtros)]
        candidatos.sort(key=lambda im: im.distancia_km if im.distancia_km is not None else 1e9)
        candidatos = candidatos[:max_detalhes]

        async def detalhar(im: Imovel):
            try:
                _detalhes(im, await fetcher.get(im.url))
            except FetchError:
                pass

        await asyncio.gather(*(detalhar(im) for im in candidatos))

        resultado = [im for im in aplica_distancia(candidatos, ref, filtros) if passa_basico(im, filtros)]
        msg = f"{paginas} páginas de listagem consultadas, {len(candidatos)} anúncios detalhados"
        return resultado, FonteStatus(fonte=self.name, ok=True, mensagem=msg, encontrados=len(resultado))
