"""Exporta uma busca (e o relatório da IA, se houver) como HTML/PDF para guardar ou compartilhar."""
import asyncio
import base64
import io
import shutil
import subprocess
import tempfile
from datetime import datetime
from html import escape
from pathlib import Path

import httpx
from PIL import Image

from .config import USER_AGENT
from .schemas import SearchResponse


async def miniaturas(urls: list[str], largura: int = 240) -> dict[str, str]:
    """Baixa cada foto e devolve {url: data-URI JPEG reduzido}. Fotos em tamanho original deixariam
    o PDF com dezenas de MB; embutidas, ele também abre offline. Foto que falhar fica de fora."""
    sem = asyncio.Semaphore(6)
    out: dict[str, str] = {}

    async def uma(cliente: httpx.AsyncClient, url: str):
        try:
            async with sem:
                r = await cliente.get(url)
            r.raise_for_status()
            img = Image.open(io.BytesIO(r.content)).convert("RGB")
            img.thumbnail((largura, largura))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=72, optimize=True)
            out[url] = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
        except Exception:  # foto quebrada/bloqueada não impede o relatório
            pass

    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as c:
        await asyncio.gather(*(uma(c, u) for u in dict.fromkeys(urls)))
    return out

ROTULO_TIPO = {"apartamento": "Apartamento", "casa": "Casa", "cobertura": "Cobertura",
               "area-privativa": "Área privativa", "kitnet": "Kitnet"}

_CSS = """
@page { size: A4; margin: 14mm 12mm; }
* { box-sizing: border-box; }
body { font: 11px/1.45 "Segoe UI", Roboto, Arial, sans-serif; color: #1c2230; margin: 0; }
h1 { font-size: 20px; margin: 0 0 4px; }
h2 { font-size: 15px; margin: 18px 0 8px; border-bottom: 2px solid #2f6fed; padding-bottom: 3px; }
.muted { color: #677085; }
.pedido { font-size: 13px; background: #f0f2f5; border-radius: 6px; padding: 8px 10px; margin: 8px 0; }
.chips span { display: inline-block; background: #eef2fd; color: #2f6fed; border-radius: 10px; padding: 1px 8px; margin: 2px 3px 2px 0; }
.chips span.off { background: #fff4dc; color: #8a5a00; }
.item { display: grid; grid-template-columns: 26px 120px 1fr; gap: 10px; padding: 8px 0; border-bottom: 1px solid #e2e5ea;
        break-inside: avoid; }
.pos { font-size: 16px; font-weight: 700; }
.foto { width: 120px; height: 82px; object-fit: cover; border-radius: 5px; background: #f0f2f5; }
.titulo { font-weight: 700; font-size: 12px; }
.preco { font-size: 14px; font-weight: 700; }
.flags span { display: inline-block; font-size: 10px; border-radius: 4px; padding: 0 5px; margin: 2px 3px 0 0; }
.sim { background: #e3f5eb; color: #1a7f4b; } .nao { background: #fde8e6; color: #b42318; } .nd { background: #eef0f3; color: #677085; }
.prio { background: #efe9ff; color: #5b3cc4; }
a { color: #2f6fed; text-decoration: none; word-break: break-all; }
.rel { background: #faf8ff; border: 1px solid #e4dcff; border-radius: 6px; padding: 10px 12px; }
.rel ul { margin: 3px 0; padding-left: 16px; }
.cols { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
"""


def _brl(v) -> str:
    return "–" if v is None else "R$ " + f"{v:,.0f}".replace(",", ".")


def _flag(rotulo: str, v) -> str:
    cls, txt = ("sim", "sim") if v is True else ("nao", "não") if v is False else ("nd", "?")
    return f'<span class="{cls}">{escape(rotulo)}: {txt}</span>'


def _filtros(resp: SearchResponse) -> str:
    f = resp.filtros
    c = [("Tipo", ", ".join(ROTULO_TIPO.get(t, t) for t in f.tipos))]
    if f.quartos_min: c.append(("Quartos", f"{f.quartos_min}+"))
    if f.bairro: c.append(("Bairro", (resp.referencia or {}).get("bairro") or f.bairro))
    if f.raio_km: c.append(("Raio", f"{f.raio_km:g} km"))
    if f.preco_max: c.append(("Aluguel até", _brl(f.preco_max)))
    if f.total_max: c.append(("Total até", _brl(f.total_max)))
    if f.vagas_min: c.append(("Vagas", f"{f.vagas_min}+"))
    if f.aceita_pet: c.append(("Pet", "aceita"))
    if f.caracteristicas: c.append(("Prioridades", ", ".join(f.caracteristicas)))
    return "".join(f"<span><b>{escape(k)}:</b> {escape(str(v))}</span>" for k, v in c)


def _onibus(im) -> str:
    """Resumo das linhas de ônibus a até 500 m (números das 12 mais próximas)."""
    o = im.onibus
    if not o or not o.get("linhas"):
        return ""
    numeros = ", ".join(l["numero"] for l in o["linhas"][:12]) + ("…" if len(o["linhas"]) > 12 else "")
    return (f'<div>🚌 {len(o["linhas"])} linhas de ônibus a até {o["raio_m"]} m '
            f'(ponto mais perto a {o["ponto_mais_proximo_m"]} m): {escape(numeros)}</div>')


def _street_view(im) -> str:
    """Link do Google Street View na coordenada do anúncio (clicável no PDF)."""
    if im.latitude is None or im.longitude is None:
        return ""
    url = f"https://www.google.com/maps/@?api=1&map_action=pano&viewpoint={im.latitude},{im.longitude}"
    return f'<div><a href="{escape(url)}">🧍 Ver no Google Street View</a></div>'


def _item(n: int, im, extra: str = "", thumbs: dict | None = None) -> str:
    url_foto = next((u for u in im.fotos if u.startswith("https://")), None)
    foto = (thumbs or {}).get(url_foto) if thumbs is not None else url_foto
    andar = "térreo" if im.andar == 0 else f"{im.andar}º andar" if im.andar else None
    specs = " · ".join(x for x in [
        f"{im.quartos} quartos" if im.quartos is not None else None,
        f"{im.area_m2:g} m²" if im.area_m2 else None,
        f"{im.vagas} vaga(s)" if im.vagas is not None else "vagas ?",
        andar,
        f"{im.distancia_km:g} km do bairro" if im.distancia_km is not None else None,
    ] if x)
    prios = "".join(f'<span class="prio">✓ {escape(a)}</span>' for a in im.atende)
    return f"""
    <div class="item">
      <div class="pos">{n}</div>
      {f'<img class="foto" src="{escape(foto)}">' if foto else '<div class="foto"></div>'}
      <div>
        <div class="titulo">{escape(ROTULO_TIPO.get(im.tipo or "", im.tipo or "Imóvel"))} · {escape(im.bairro or "")}</div>
        <div class="muted">{escape(im.endereco or "")}</div>
        <div><span class="preco">{_brl(im.total_mensal or im.aluguel)}</span>/mês
          <span class="muted">(aluguel {_brl(im.aluguel)} · cond. {_brl(im.condominio)} · IPTU {_brl(im.iptu)})</span></div>
        <div>{escape(specs)}</div>
        {_onibus(im)}
        <div class="flags">{_flag("Pet", im.aceita_pet)}{_flag("Seguro fiança", im.seguro_fianca)}{_flag("Fiador", im.fiador)}{_flag("Caução", im.caucao)}{prios}</div>
        {extra}
        <div><a href="{escape(im.url)}">{escape(im.url)}</a> <span class="muted">({escape(im.fonte)})</span></div>
        {_street_view(im)}
      </div>
    </div>"""


def _relatorio(rel: dict, por_id: dict, thumbs: dict | None) -> str:
    def lista(itens):
        return "<ul>" + "".join(f"<li>{escape(x)}</li>" for x in itens) + "</ul>" if itens else ""
    itens = []
    for n, r in enumerate(rel["ranking"], 1):
        im = por_id.get(r["id"])
        if not im:
            continue
        extra = f"""<div class="rel"><b>{escape(r['titulo_curto'])} — nota {r['nota']}/10.</b> {escape(r['por_que'])}
          <div class="cols"><div><b>Pontos fortes</b>{lista(r['pontos_fortes'])}</div>
          <div><b>Atenção</b>{lista(r['pontos_de_atencao'])}</div>
          <div><b>Perguntar</b>{lista(r['perguntas_para_o_anunciante'])}</div></div></div>"""
        itens.append(_item(n, im, extra, thumbs))
    passos = lista(rel.get("proximos_passos") or [])
    prios = " · ".join(f"<b>{escape(p['criterio'])}</b> ({escape(p['peso'])})" for p in rel.get("prioridades") or [])
    if rel.get("observacao"):
        prios += (" · " if prios else "") + f"“{escape(rel['observacao'])}”"
    return f"""<h2>Relatório da IA — os {len(itens)} mais alinhados</h2>
      {f'<p class="muted">Priorizando: {prios}</p>' if prios else ''}
      <p>{escape(rel['resumo'])}</p>{''.join(itens)}
      <h2>Comparativo</h2><p>{escape(rel['comparativo'])}</p>
      {f'<h2>Próximos passos</h2>{passos}' if passos else ''}"""


def html_busca(resp: SearchResponse, relatorio: dict | None = None, top: int = 30,
               thumbs: dict | None = None) -> str:
    """`thumbs` = {url: data-URI} de `miniaturas`; sem ele, as fotos apontam para a internet."""
    por_id = {f"{im.fonte}:{im.id}": im for im in resp.imoveis}
    fontes = "".join(f'<span class="{"" if s.ok else "off"}">{escape(s.fonte)}: '
                     f'{s.encontrados if s.ok else "bloqueado"}</span>' for s in resp.fontes)
    lista = "".join(_item(n, im, thumbs=thumbs) for n, im in enumerate(resp.imoveis[:top], 1))
    return f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><title>House - busca</title>
<style>{_CSS}</style></head><body>
  <h1>House · imóveis para alugar em BH</h1>
  <div class="muted">Gerado em {datetime.now():%d/%m/%Y %H:%M} · {resp.total} imóveis encontrados</div>
  <div class="pedido">“{escape(resp.texto)}”</div>
  <div class="chips">{_filtros(resp)}</div>
  <div class="chips">{fontes}</div>
  {_relatorio(relatorio, por_id, thumbs) if relatorio else ''}
  <h2>{'Todos os resultados' if relatorio else 'Resultados'} — {min(top, resp.total)} de {resp.total}, na ordem de prioridade</h2>
  {lista}
  <p class="muted">Dados copiados dos anúncios; confirme valores e condições com o anunciante.</p>
</body></html>"""


async def html_busca_com_fotos(resp: SearchResponse, relatorio: dict | None = None,
                               top: int = 30) -> tuple[str, int, int]:
    """Como html_busca, com as fotos reduzidas e embutidas (para PDF leve e offline).
    Devolve também quantas fotos foram embutidas e quantas foram tentadas."""
    ids = {r["id"] for r in (relatorio or {}).get("ranking", [])}
    listados = resp.imoveis[:top] + [im for im in resp.imoveis if f"{im.fonte}:{im.id}" in ids]
    urls = list(dict.fromkeys(u for im in listados for u in im.fotos[:1] if u.startswith("https://")))
    thumbs = await miniaturas(urls)
    return html_busca(resp, relatorio, top, thumbs), len(thumbs), len(urls)


def _navegador() -> str | None:
    candidatos = [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                  r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                  r"C:\Program Files\Google\Chrome\Application\chrome.exe"]
    return next((c for c in candidatos if Path(c).exists()), None) or shutil.which("msedge") or shutil.which("chrome")


def salvar_pdf(html: str, destino: Path) -> Path:
    """Converte o HTML em PDF com Edge/Chrome sem janela. Também salva o .html ao lado."""
    destino = destino.resolve()
    destino.parent.mkdir(parents=True, exist_ok=True)
    pagina = destino.with_suffix(".html")
    pagina.write_text(html, encoding="utf-8")
    nav = _navegador()
    if not nav:
        raise RuntimeError(f"Edge/Chrome não encontrado; abra {pagina} no navegador e imprima em PDF.")
    with tempfile.TemporaryDirectory() as perfil:  # perfil separado: não mexe no navegador aberto
        subprocess.run([nav, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                        f"--user-data-dir={perfil}", "--virtual-time-budget=15000",
                        f"--print-to-pdf={destino}", pagina.as_uri()],
                       check=True, timeout=180, capture_output=True)
    if not destino.exists():
        raise RuntimeError(f"O navegador não gerou o PDF; abra {pagina} e imprima em PDF.")
    return destino
