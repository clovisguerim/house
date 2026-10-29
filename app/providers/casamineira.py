import asyncio
import json
import re

from ..parser import normalize
from ..schemas import Filtros, FonteStatus, Imovel
from .base import (FetchError, Fetcher, Provider, Referencia, aplica_distancia, infer_andar, infer_garantias,
                   infer_pet, money, passa_basico)

BASE = "https://www.casamineira.com.br"
CIDADE = "belo-horizonte_mg"
# Cobertura e área privativa não têm listagem própria: vêm da listagem de apartamentos
TIPO_SLUG = {"apartamento": "apartamento", "casa": "casa", "cobertura": "apartamento", "area-privativa": "apartamento"}


def _estado(html: str) -> dict | None:
    """O site (plataforma Navent) embute a busca em window.__PRELOADED_STATE__."""
    i = html.find("window.__PRELOADED_STATE__")
    if i < 0:
        return None
    try:
        estado, _ = json.JSONDecoder().raw_decode(html[html.find("{", i):])
    except json.JSONDecodeError:
        return None
    return estado


def _feature(p: dict, fid: str) -> float | None:
    v = ((p.get("mainFeatures") or {}).get(fid) or {}).get("value")
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def _tipo(p: dict) -> str:
    if (p.get("realEstateType") or {}).get("realEstateTypeId") == "1":
        return "casa"
    titulo = normalize(f"{p.get('generatedTitle') or ''} {p.get('title') or ''}")
    if "cobertura" in titulo:
        return "cobertura"
    if re.search(r"\barea privativa\b", titulo):
        return "area-privativa"
    return "apartamento"


def _imovel(p: dict) -> Imovel | None:
    aluguel = None
    for op in p.get("priceOperationTypes") or []:
        if (op.get("operationType") or {}).get("operationTypeId") == "2" and op.get("prices"):
            aluguel = op["prices"][0].get("amount")
    if not p.get("postingId") or not p.get("url"):
        return None

    loc = p.get("postingLocation") or {}
    bairro = (loc.get("location") or {}).get("name")
    rua = ((loc.get("address") or {}).get("name") or "").strip()
    geo = (loc.get("postingGeolocation") or {}).get("geolocation") or {}
    desc = p.get("descriptionNormalized") or ""

    iptu = (p.get("iptu") or {}).get("amount") if isinstance(p.get("iptu"), dict) else p.get("iptu")
    if iptu is None and (m := re.search(r"iptu:?\s*r\$\s*([\d.,]+)", normalize(desc))):
        iptu = money(m.group(1))

    caracteristicas = [
        f.get("label") for grupo in ("generalFeatures", "developmentFeatures") for cat in (p.get(grupo) or {}).values()
        for f in (cat.values() if isinstance(cat, dict) else []) if isinstance(f, dict) and f.get("label")
    ] + [f if isinstance(f, str) else f.get("label", "") for f in (p.get("highlightedFeatures") or []) + (p.get("flagsFeatures") or [])]
    caracteristicas = [c for c in caracteristicas if c]

    texto = " ".join([desc, *caracteristicas])
    andar, andar_alto = infer_andar(texto)
    g = infer_garantias(texto)
    quartos, banheiros, vagas = _feature(p, "CFT2"), _feature(p, "CFT3"), _feature(p, "CFT7")
    fotos = [f.get("url730x532") for f in ((p.get("visiblePictures") or {}).get("pictures") or [])]

    return Imovel(
        fonte="casamineira",
        id=str(p["postingId"]),
        url=BASE + p["url"] if p["url"].startswith("/") else p["url"],
        titulo=p.get("generatedTitle") or p.get("title"),
        tipo=_tipo(p),
        endereco=", ".join(x for x in (rua, bairro, "Belo Horizonte - MG") if x),
        bairro=bairro,
        latitude=geo.get("latitude"),
        longitude=geo.get("longitude"),
        quartos=int(quartos) if quartos is not None else None,
        banheiros=int(banheiros) if banheiros is not None else None,
        area_m2=_feature(p, "CFT101") or _feature(p, "CFT100"),
        aluguel=aluguel,
        condominio=(p.get("expenses") or {}).get("amount"),
        iptu=iptu,
        # Anúncio sem o campo de vagas é tratado como sem vaga, igual ao card do site
        vagas=int(vagas) if vagas is not None else 0,
        andar=andar,
        andar_alto=andar_alto,
        aceita_pet=infer_pet(texto),
        descricao=desc or None,
        seguro_fianca=g["seguro_fianca"],
        fiador=g["fiador"],
        caucao=g["caucao"],
        titulo_capitalizacao=g["titulo_capitalizacao"],
        caracteristicas=caracteristicas,
        fotos=[f for f in fotos if f and f.startswith("https://")],
    )


class CasaMineiraProvider(Provider):
    """Casa Mineira (plataforma Navent/QuintoAndar).

    Os dados da listagem (preço, condomínio, coordenadas do bairro, quartos, vagas, descrição) vêm
    embutidos no HTML, então não é preciso abrir cada anúncio. O site usa Cloudflare e pode
    desafiar clientes automatizados; nesse caso a fonte é reportada como bloqueada, sem contorno.
    """

    name = "casamineira"
    label = "Casa Mineira"

    async def search(self, filtros: Filtros, fetcher: Fetcher, max_detalhes: int, ref: Referencia | None):
        q = filtros.quartos_min
        sufixo = f"/{q}-quarto{'s' if q > 1 else ''}" if q else ""
        locais = [f"{ref['slug']}_{CIDADE}", CIDADE] if ref else [CIDADE]
        tipos = sorted({TIPO_SLUG[t] for t in filtros.tipos if t in TIPO_SLUG})
        urls = [f"{BASE}/aluguel/{t}/{loc}{sufixo}" for t in tipos for loc in locais]

        bloqueios: list[int] = []

        async def listar(url: str) -> list[Imovel]:
            try:
                html = await fetcher.get(url)
            except FetchError as e:
                bloqueios.append(e.status)
                return []
            estado = _estado(html) or {}
            posts = (estado.get("listStore") or {}).get("listPostings") or []
            return [im for p in posts if (im := _imovel(p))]

        vistos: dict[str, Imovel] = {}
        for lote in await asyncio.gather(*(listar(u) for u in urls)):
            for im in lote:
                vistos.setdefault(im.id, im)

        if bloqueios and len(bloqueios) == len(urls):
            msg = ("Casa Mineira bloqueou o acesso automatizado (Cloudflare)" if all(s == 403 for s in bloqueios)
                   else f"Casa Mineira respondeu HTTP {bloqueios[0]}")
            return [], FonteStatus(fonte=self.name, ok=False, mensagem=msg)

        candidatos = [im for im in vistos.values() if passa_basico(im, filtros)]
        resultado = [im for im in aplica_distancia(candidatos, ref, filtros) if passa_basico(im, filtros)]
        msg = f"{len(urls) - len(bloqueios)}/{len(urls)} páginas de listagem lidas, {len(vistos)} anúncios"
        return resultado, FonteStatus(fonte=self.name, ok=True, mensagem=msg, encontrados=len(resultado))
