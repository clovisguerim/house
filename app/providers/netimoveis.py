import asyncio
import difflib
import json
import re
from pathlib import Path
from html import unescape
from urllib.parse import urlencode

from ..geo import haversine_km
from ..parser import slugify
from ..schemas import Filtros, FonteStatus, Imovel
from .base import (FetchError, Fetcher, Provider, Referencia, aplica_distancia, infer_andar, infer_garantias,
                   infer_pet, json_ld_blocks, money, passa_basico, strip_tags)

BASE = "https://www.netimoveis.com"
CIDADE = "locacao/minas-gerais/belo-horizonte"
REGIOES = ["barreiro", "centro-sul", "leste", "nordeste", "noroeste", "norte", "oeste", "pampulha", "venda-nova"]
TIPO_SLUG = {"apartamento": "apartamento", "casa": "casa", "cobertura": "cobertura", "kitnet": "kitnet",
             "area-privativa": "area-privativa"}
# Margem para manter uma região cujo centro está fora do raio, mas parte dela não
MARGEM_REGIAO_KM = 7


def _localizacao(html: str) -> dict | None:
    m = re.search(r"window\.localizacoesSSR\s*=\s*(\[.*?\]);", html)
    if not m:
        return None
    try:
        locs = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    return locs[0] if locs else None


def _int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text)
    return int(m.group(1)) if m else None


def _cards(html: str) -> list[Imovel]:
    """Lê os anúncios do JSON-LD (ItemList) de uma página de listagem."""
    out = []
    for block in json_ld_blocks(html):
        if not isinstance(block, dict) or block.get("@type") != "ItemList":
            continue
        for el in block.get("itemListElement", []):
            item = el.get("item", {})
            offer = item.get("offers", {})
            prop = offer.get("itemOffered", {})
            url = (item.get("url") or "").split("?")[0]
            m = re.search(r"/imovel/locacao-([a-z-]+?)-minas-gerais-belo-horizonte-([a-z0-9-]+)/(\d+)", url)
            if not m:
                continue
            nome = item.get("name") or ""
            condo = prop.get("additionalProperty") or {}
            if isinstance(condo, list):
                condo = next((p for p in condo if "Condo" in p.get("name", "")), {})
            vagas = _int(r"(\d+)\s+vagas?", nome)
            out.append(Imovel(
                fonte="netimoveis",
                id=m.group(3),
                url=url,
                titulo=nome,
                tipo=m.group(1),
                endereco=(prop.get("address") or {}).get("streetAddress"),
                bairro=m.group(2).replace("-", " ").title(),
                quartos=prop.get("numberOfBedrooms"),
                banheiros=prop.get("numberOfBathroomsTotal"),
                area_m2=(prop.get("floorSize") or {}).get("value"),
                aluguel=offer.get("price"),
                condominio=condo.get("value"),
                vagas=vagas if vagas is not None else (None if "vaga" in nome.lower() else 0),
                fotos=item.get("image") or [],
            ))
    return out


def _detalhes(im: Imovel, html: str) -> None:
    for block in json_ld_blocks(html):
        for node in (block.get("@graph", []) if isinstance(block, dict) else []):
            if node.get("@type") != "RealEstateListing":
                continue
            prop = (node.get("offers") or {}).get("itemOffered") or {}
            geo = prop.get("geo") or {}
            im.latitude, im.longitude = geo.get("latitude"), geo.get("longitude")
            if addr := (prop.get("address") or {}).get("streetAddress"):
                im.endereco = addr
            if fotos := prop.get("image"):
                im.fotos = fotos

    def campo(rotulo: str) -> float | None:
        m = re.search(rf">{rotulo}:</span>\s*<span[^>]*>([^<]+)<", html)
        return money(m.group(1)) if m else None

    im.condominio = campo(r"Condom[^:<]*") or im.condominio
    im.iptu = campo("IPTU")
    im.aluguel = campo("Aluguel") or im.aluguel
    if (v := _int(r'alt="Vagas"[^>]*>\s*(\d+)', html)) is not None:
        im.vagas = v
    im.caracteristicas = [unescape(f).strip() for f in re.findall(r'pn-d-s06-flag[^"]*">([^<]+)<', html)]

    desc = ""
    if m := re.search(r'class="[^"]*prose-blockquote[^"]*">(.*?)</div>', html, re.S):
        desc = strip_tags(m.group(1))
    texto = " ".join([desc, *im.caracteristicas])
    im.descricao = desc or None
    im.andar, im.andar_alto = infer_andar(texto)
    im.aceita_pet = infer_pet(texto)
    g = infer_garantias(texto)
    im.seguro_fianca, im.fiador, im.caucao, im.titulo_capitalizacao = (
        g["seguro_fianca"], g["fiador"], g["caucao"], g["titulo_capitalizacao"])


_TABELA = Path(__file__).resolve().parent.parent / "data" / "bairros_bh.json"
_BAIRROS: dict[str, dict] = json.loads(_TABELA.read_text(encoding="utf-8")) if _TABELA.exists() else {}


def _bairro_da_tabela(bairro: str) -> Referencia | None:
    """Procura na tabela embutida (gerada a partir do Netimóveis): slug exato, depois nome
    parecido, para aceitar erro de digitação ("gloria", "Glória", "glorya")."""
    slug = slugify(bairro)
    if slug not in _BAIRROS:
        parecidos = difflib.get_close_matches(slug, list(_BAIRROS), n=1, cutoff=0.8)
        if not parecidos:
            return None
        slug = parecidos[0]
    b = _BAIRROS[slug]
    return {"bairro": b["bairro"], "slug": slug, "regiao": b["regiao"], "regiao_slug": b["regiao_slug"],
            "latitude": b["latitude"], "longitude": b["longitude"]}


async def localizar_bairro(bairro: str, fetcher: Fetcher) -> Referencia | None:
    """Coordenadas e região de um bairro de BH.

    Primeiro a tabela embutida (não depende de rede: servidores na nuvem podem ser bloqueados pelos
    portais); se o bairro não estiver lá, a página de bairro do Netimóveis, que resolve a região a
    partir do bairro, então qualquer segmento de região serve na URL.
    """
    if ref := _bairro_da_tabela(bairro):
        return ref
    try:
        html = await fetcher.get(f"{BASE}/{CIDADE}/bairro/{slugify(bairro)}")
    except FetchError:
        return None
    loc = _localizacao(html)
    if not loc or loc.get("tipo") != "Bairro":
        return None
    return {"bairro": loc["bairroMigalha"], "slug": loc["bairro"], "regiao": loc["regiaoMigalha"],
            "regiao_slug": loc["regiao"], "latitude": loc["latitude"], "longitude": loc["longitude"]}


class NetimoveisProvider(Provider):
    name = "netimoveis"
    label = "Netimóveis"

    async def search(self, filtros: Filtros, fetcher: Fetcher, max_detalhes: int, ref: Referencia | None):
        qs = f"?{urlencode({'quartos': filtros.quartos_min})}" if filtros.quartos_min else ""
        if ref and filtros.raio_km:
            locais = [f"{ref['regiao_slug']}/{ref['slug']}", *REGIOES]
        elif ref:
            locais = [f"{ref['regiao_slug']}/{ref['slug']}"]
        else:
            locais = [""]
        urls = [
            f"{BASE}/{CIDADE}/{loc + '/' if loc else ''}{TIPO_SLUG[t]}{qs}"
            for t in filtros.tipos if t in TIPO_SLUG for loc in locais
        ]

        async def listar(url: str) -> list[Imovel]:
            try:
                html = await fetcher.get(url)
            except FetchError:
                return []
            loc = _localizacao(html)
            if ref and filtros.raio_km and loc and loc.get("tipo") == "Regiao":
                d = haversine_km(ref["latitude"], ref["longitude"], loc["latitude"], loc["longitude"])
                if d > filtros.raio_km + MARGEM_REGIAO_KM:
                    return []
            return _cards(html)

        vistos: dict[str, Imovel] = {}
        for lote in await asyncio.gather(*(listar(u) for u in urls)):
            for im in lote:
                vistos.setdefault(im.id, im)
        candidatos = [im for im in vistos.values() if passa_basico(im, filtros)]

        # Anúncios do próprio bairro primeiro, caso max_detalhes corte a lista
        if ref:
            candidatos.sort(key=lambda im: slugify(im.bairro or "") != ref["slug"])
        candidatos = candidatos[:max_detalhes]

        async def detalhar(im: Imovel):
            try:
                _detalhes(im, await fetcher.get(im.url))
            except FetchError:
                pass

        await asyncio.gather(*(detalhar(im) for im in candidatos))

        resultado = [im for im in aplica_distancia(candidatos, ref, filtros) if passa_basico(im, filtros)]
        msg = f"{len(urls)} páginas de listagem consultadas, {len(candidatos)} anúncios detalhados"
        return resultado, FonteStatus(fonte=self.name, ok=True, mensagem=msg, encontrados=len(resultado))
