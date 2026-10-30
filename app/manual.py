"""Anúncio adicionado à mão pelo link (ex.: Viva Real, ZAP, OLX, site de imobiliária).

Se o site deixa abrir a página, os dados vêm dela; se bloqueia (Cloudflare etc.), a pessoa cola o texto
do anúncio que ela mesma abriu no navegador. A IA extrai os campos; a pessoa pode corrigir à mão.
Localização: coordenadas publicadas na página ou, senão, o centro do bairro (marcado como aproximado).
"""
import hashlib
import json
import re
from html import unescape
from urllib.parse import urlparse

import httpx

from . import llm, onibus
from .config import USER_AGENT
from .geo import haversine_km
from .parser import desejos_atendidos
from .providers.base import infer_andar, infer_garantias, infer_pet, json_ld_blocks, strip_tags
from .providers.netimoveis import _bairro_da_tabela
from .schemas import Imovel, SearchResponse

FONTES = {"vivareal.com.br": "vivareal", "zapimoveis.com.br": "zap", "olx.com.br": "olx",
          "imovelweb.com.br": "imovelweb", "quintoandar.com.br": "quintoandar", "netimoveis.com": "netimoveis",
          "chavesnamao.com.br": "chavesnamao", "casamineira.com.br": "casamineira", "lopes.com.br": "lopes",
          "loft.com.br": "loft"}
# Caixa de Belo Horizonte e arredores, para não pegar coordenada de outra coisa na página
_BH = (-20.15, -19.65, -44.20, -43.75)
CAMPOS_MANUAIS = ("aluguel", "condominio", "iptu", "quartos", "vagas", "area_m2", "tipo", "bairro", "endereco")


class AnuncioErro(Exception):
    pass


def fonte_do_link(url: str) -> str:
    host = (urlparse(url).hostname or "").removeprefix("www.")
    return next((f for d, f in FONTES.items() if host.endswith(d)), host or "link")


async def _baixar(url: str) -> tuple[str | None, str | None]:
    """HTML da página, ou (None, motivo). Uma requisição comum: se o site bloquear, não insiste."""
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                     headers={"User-Agent": USER_AGENT, "Accept-Language": "pt-BR"}) as c:
            r = await c.get(url)
    except httpx.HTTPError:
        return None, "Não consegui abrir o link."
    if r.status_code in (401, 403, 429, 503):
        return None, f"O site bloqueia acesso automatizado (HTTP {r.status_code})."
    if r.status_code != 200:
        return None, f"O site respondeu HTTP {r.status_code}."
    return r.text, None


def _coords_jsonld(obj) -> tuple[float, float] | None:
    """Primeiro par latitude/longitude dentro de BH num bloco JSON-LD (busca recursiva)."""
    if isinstance(obj, dict):
        lat, lon = obj.get("latitude"), obj.get("longitude")
        try:
            lat, lon = float(lat), float(lon)
            if _BH[0] <= lat <= _BH[1] and _BH[2] <= lon <= _BH[3]:
                return lat, lon
        except (TypeError, ValueError):
            pass
        for v in obj.values():
            if achou := _coords_jsonld(v):
                return achou
    elif isinstance(obj, list):
        for v in obj:
            if achou := _coords_jsonld(v):
                return achou
    return None


def _conteudo_pagina(html: str) -> tuple[str, dict]:
    """Texto útil para a IA (dados estruturados + texto visível) e extras (coordenadas, foto)."""
    blocos = json_ld_blocks(html)
    extras: dict = {}
    for b in blocos:
        if c := _coords_jsonld(b):
            extras["coords"] = c
            break
    meta = dict(re.findall(r'<meta[^>]+(?:property|name)="(og:[a-z:]+|description)"[^>]+content="([^"]*)"', html))
    if img := meta.get("og:image"):
        extras["foto"] = unescape(img)
    corpo = re.sub(r"<(script|style|noscript|svg)\b.*?</\1>", " ", html, flags=re.S | re.I)
    partes = [f"{k}: {unescape(v)}" for k, v in meta.items() if k in ("og:title", "og:description", "description")]
    if blocos:
        partes.append("DADOS ESTRUTURADOS: " + json.dumps(blocos, ensure_ascii=False)[:8000])
    partes.append("TEXTO DA PÁGINA: " + strip_tags(corpo)[:20000])
    return "\n".join(partes), extras


def _avisos(im: Imovel, busca: SearchResponse) -> list[str]:
    """O que o anúncio não atende nos filtros da busca (ele entra mesmo assim, com o aviso)."""
    f, out = busca.filtros, []
    aceitos = set(f.tipos) | ({"cobertura", "area-privativa"} if "apartamento" in f.tipos else set())
    if im.tipo and not any(im.tipo.startswith(t) for t in aceitos):
        out.append(f"tipo {im.tipo} fora do pedido")
    if f.quartos_min and im.quartos is not None and im.quartos < f.quartos_min:
        out.append(f"{im.quartos} quarto(s), pedido {f.quartos_min}+")
    if f.preco_max and im.aluguel and im.aluguel > f.preco_max:
        out.append(f"aluguel acima de R$ {f.preco_max:,.0f}".replace(",", "."))
    if f.total_max and im.total_mensal and im.total_mensal > f.total_max:
        out.append(f"total acima de R$ {f.total_max:,.0f}".replace(",", "."))
    if f.vagas_min and im.vagas is not None and im.vagas < f.vagas_min:
        out.append("sem a vaga pedida")
    if f.aceita_pet and im.aceita_pet is False:
        out.append("não aceita pet")
    if f.raio_km and im.distancia_km is not None and im.distancia_km > f.raio_km:
        out.append(f"a {im.distancia_km:g} km, fora do raio de {f.raio_km:g} km")
    return out


async def analisar(url: str, texto_colado: str | None, campos: dict, busca: SearchResponse) -> Imovel:
    url = url.strip()
    if not re.match(r"^https?://", url):
        raise AnuncioErro("Informe o link completo do anúncio (começando com https://).")
    texto_colado = (texto_colado or "").strip()
    html = None
    if not texto_colado or len(texto_colado) < 200:
        html, motivo = await _baixar(url)
        if html is None and not texto_colado:
            raise AnuncioErro(f"{motivo} Abra o anúncio no seu navegador, copie a página inteira "
                              "(Ctrl+A, Ctrl+C) e cole no campo de texto.")
    conteudo, extras = _conteudo_pagina(html) if html else ("", {})
    if texto_colado:
        conteudo = f"TEXTO COPIADO PELA PESSOA: {texto_colado[:25000]}\n\n{conteudo}"

    campos = {k: v for k, v in (campos or {}).items() if k in CAMPOS_MANUAIS and v not in (None, "")}
    if llm.ativa():
        try:
            dados = await llm.extrair_anuncio(f"URL: {url}\n\n{conteudo}")
        except (llm.ErroIA, ValueError) as e:
            raise AnuncioErro(f"A IA não conseguiu ler o anúncio ({e}).")
        if not dados.pop("e_anuncio_de_aluguel") and "aluguel" not in campos:
            raise AnuncioErro("O conteúdo não parece um anúncio de imóvel para alugar. Confira o link ou cole o "
                              "texto da página do anúncio.")
    else:
        if "aluguel" not in campos:
            raise AnuncioErro("A IA está desligada: preencha ao menos o aluguel em 'Corrigir à mão'.")
        g = infer_garantias(conteudo)
        dados = {"aceita_pet": infer_pet(conteudo), "andar": infer_andar(conteudo)[0], "destaques": [],
                 "caracteristicas": [], **g}
    dados.update(campos)  # o que a pessoa digitou vale mais que a leitura

    ref = busca.referencia
    coords, aproximado = extras.get("coords"), False
    if not coords and dados.get("bairro") and (b := _bairro_da_tabela(dados["bairro"])):
        coords, aproximado = (b["latitude"], b["longitude"]), True

    bairro = dados.get("bairro")
    endereco = ", ".join(x for x in (dados.get("endereco"), bairro, "Belo Horizonte - MG") if x)
    im = Imovel(
        fonte=fonte_do_link(url), id="m" + hashlib.sha1(url.encode()).hexdigest()[:10], url=url,
        titulo=dados.get("titulo"), tipo=dados.get("tipo"), endereco=endereco, bairro=bairro,
        latitude=coords[0] if coords else None, longitude=coords[1] if coords else None,
        quartos=dados.get("quartos"), banheiros=dados.get("banheiros"), area_m2=dados.get("area_m2"),
        aluguel=dados.get("aluguel"), condominio=dados.get("condominio"), iptu=dados.get("iptu"),
        vagas=dados.get("vagas"), andar=dados.get("andar"), aceita_pet=dados.get("aceita_pet"),
        **{g: dados.get(g) for g in llm.GARANTIAS}, mobiliado=dados.get("mobiliado"),
        destaques=(dados.get("destaques") or [])[:4], caracteristicas=(dados.get("caracteristicas") or [])[:40],
        fotos=[extras["foto"]] if extras.get("foto", "").startswith("https://") else [],
        manual=True, local_aproximado=aproximado, descricao=conteudo[:3000],
    )
    if im.aluguel is not None:
        im.total_mensal = im.aluguel + (im.condominio or 0)
    im.tem_estacionamento = None if im.vagas is None else im.vagas > 0
    if ref and im.latitude is not None:
        im.distancia_km = round(haversine_km(ref["latitude"], ref["longitude"], im.latitude, im.longitude), 2)
    if im.latitude is not None:
        im.onibus = onibus.linhas_proximas(im.latitude, im.longitude)
    im.atende = desejos_atendidos(" ".join([conteudo, *im.caracteristicas, *im.destaques]),
                                  busca.filtros.caracteristicas)
    im.avisos = _avisos(im, busca)
    return im
