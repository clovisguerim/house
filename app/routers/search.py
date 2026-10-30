import asyncio
import json
import math
import re
import time
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import SearchHistory, User
from ..parser import desejos_atendidos, parse_query
from ..providers.base import FetchError, Fetcher
from .. import llm
from ..config import BUSCAS_POR_HORA, USER_AGENT
from ..geo import haversine_km
from ..providers.casamineira import CasaMineiraProvider
from ..providers.chavesnamao import ChavesNaMaoProvider
from ..providers.lopes import LopesProvider
from ..providers.netimoveis import _BAIRROS, NetimoveisProvider, localizar_bairro
from ..providers.quintoandar import QuintoAndarProvider
from ..providers.vivareal import VivaRealProvider
from ..schemas import Filtros, FonteStatus, HistoricoItem, Imovel, SearchRequest, SearchResponse
from ..security import get_current_user, require_admin
from .regras import regras_para

router = APIRouter(tags=["busca"])

PROVIDERS = {p.name: p for p in (NetimoveisProvider(), QuintoAndarProvider(), ChavesNaMaoProvider(), LopesProvider(),
                                  CasaMineiraProvider(), VivaRealProvider())}


def _filtros_finais(imoveis: list[Imovel], f: Filtros, so_dados: bool = False) -> list[Imovel]:
    """Filtros que dependem dos detalhes do anúncio. Anúncios que não informam (null) são mantidos;
    só sai quem diz explicitamente que não atende.

    `so_dados=True` aplica só o que não depende do texto do anúncio (tipo, preço total), para a IA
    não ler anúncio que vai ser descartado de qualquer jeito."""
    out = []
    for im in imoveis:
        # A listagem de apartamentos também traz coberturas/áreas privativas: respeita o tipo pedido
        aceitos = set(f.tipos) | ({"cobertura", "area-privativa"} if "apartamento" in f.tipos else set())
        if im.tipo and not any(im.tipo.startswith(t) for t in aceitos):
            continue
        im.tem_estacionamento = None if im.vagas is None else im.vagas > 0
        if im.aluguel is not None:
            # Mesmo critério do site: aluguel + condomínio (IPTU fica em campo separado)
            im.total_mensal = im.aluguel + (im.condominio or 0)
            if f.total_max and im.total_mensal > f.total_max:
                continue
        if not so_dados:
            # Andar só restringe apartamento comum ("área privativa ou cobertura ou 1º andar")
            if f.andar is not None and im.tipo == "apartamento" and im.andar != f.andar:
                continue
            if im.tipo == "apartamento" and im.andar is not None and im.andar in f.andares_excluidos:
                continue
            if f.aceita_pet and im.aceita_pet is False:
                continue
            if f.garantias and all(getattr(im, g) is False for g in f.garantias):
                continue
        out.append(im)
    return out


def _deduplicar(imoveis: list[Imovel]) -> list[Imovel]:
    """Junta o mesmo imóvel anunciado em portais diferentes: aluguel e quartos iguais e a até 150 m
    (cada portal geocodifica o endereço de um jeito). Mantém o primeiro anúncio e completa campos
    vazios com os dos repetidos."""
    grupos: dict[tuple, list[Imovel]] = {}
    out = []
    for im in imoveis:
        if im.latitude is None or im.aluguel is None:
            out.append(im)
            continue
        candidatos = grupos.setdefault((im.aluguel, im.quartos), [])
        base = next((b for b in candidatos if b.fonte != im.fonte
                     and im.fonte not in {o["fonte"] for o in b.outras_fontes}
                     and haversine_km(b.latitude, b.longitude, im.latitude, im.longitude) <= 0.15), None)
        if base is None:
            candidatos.append(im)
            out.append(im)
            continue
        base.outras_fontes.append({"fonte": im.fonte, "url": im.url})
        for campo in ("condominio", "iptu", "vagas", "andar", "aceita_pet", "seguro_fianca", "fiador", "caucao",
                      "titulo_capitalizacao", "area_m2", "banheiros"):
            if getattr(base, campo) is None and getattr(im, campo) is not None:
                setattr(base, campo, getattr(im, campo))
    return out


def _ordem(im: Imovel, f: Filtros):
    # Quem confirma pet/garantia/desejos pedidos vem antes; depois distância e preço
    confirma = (bool(f.aceita_pet and im.aceita_pet is True) + sum(getattr(im, g) is True for g in f.garantias)
                + bool(f.vagas_min and im.vagas) + len(im.atende))
    return (-confirma, im.distancia_km if im.distancia_km is not None else 999, im.total_mensal or 1e9)


async def _interpretar(texto: str, regras: list[str]) -> tuple[Filtros, str | None]:
    """IA quando disponível (seguindo as regras de preferência); regras fixas do parser como reserva.
    Devolve também o erro da IA, se houve."""
    if llm.ativa():
        try:
            return await llm.interpretar(texto, regras), None
        except (llm.ErroIA, ValueError) as e:  # ValueError: JSON/validação
            return parse_query(texto), f"interpretação por IA falhou ({e}); usadas as regras"
    return parse_query(texto), None


_em_andamento: set[int] = set()
_historico_uso: dict[int, list[float]] = {}


def _checar_limite(user: User) -> None:
    """Uma busca por vez por usuário e no máximo BUSCAS_POR_HORA (admins sem limite por hora).

    Em memória: suficiente para um processo só, que é como o app roda."""
    if user.id in _em_andamento:
        raise HTTPException(status_code=429, detail="Você já tem uma busca em andamento. Aguarde ela terminar.")
    if user.is_admin:
        return
    agora = time.time()
    recentes = [t for t in _historico_uso.get(user.id, []) if agora - t < 3600]
    if len(recentes) >= BUSCAS_POR_HORA:
        espera = math.ceil((3600 - (agora - recentes[0])) / 60)
        raise HTTPException(status_code=429, detail=f"Limite de {BUSCAS_POR_HORA} buscas por hora. Tente em {espera} min.")
    _historico_uso[user.id] = recentes + [agora]


@router.post("/buscar", response_model=SearchResponse)
async def buscar(req: SearchRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _checar_limite(user)
    _em_andamento.add(user.id)
    try:
        return await _buscar(req, user, db)
    finally:
        _em_andamento.discard(user.id)


async def _buscar(req: SearchRequest, user: User, db: Session) -> SearchResponse:
    anterior = None
    if req.comparar_com:
        row = db.get(SearchHistory, req.comparar_com)
        if row and row.results_json and (row.user_id == user.id or user.is_admin):
            anterior = (row.id, row.created_at, SearchResponse.model_validate_json(row.results_json))
    resposta = await executar_busca(req, regras_para(db, user), anterior)
    return salvar_busca(db, user, resposta)


# Texto de página de anúncio que já não está disponível
_FORA_DO_AR = re.compile(r"n[aã]o est[aá] mais dispon[ií]vel|indispon[ií]vel|j[aá] foi alugado|an[uú]ncio "
                         r"(removido|encerrado|expirado|desativado)|im[oó]vel n[aã]o encontrado|p[aá]gina n[aã]o encontrada",
                         re.IGNORECASE)


async def _status_anuncio(cliente: httpx.AsyncClient, im: Imovel) -> tuple[str, str]:
    """Se um anúncio que sumiu da busca ainda está no ar. Não segue redirecionamentos: um anúncio
    removido costuma redirecionar para a listagem ou para uma página de "indisponível"."""
    try:
        r = await cliente.get(im.url)
    except httpx.HTTPError:
        return "nao_verificado", "sem resposta do portal"
    if r.status_code in (404, 410):
        return "fora_do_ar", f"o portal respondeu {r.status_code}"
    if r.status_code in (403, 429):
        return "nao_verificado", "o portal bloqueou a verificação"
    if 300 <= r.status_code < 400:
        destino = r.headers.get("location", "")
        if im.id not in destino or "indisponivel" in destino:
            return "fora_do_ar", "o anúncio redireciona para outra página"
        return "no_ar", "anúncio ainda publicado"
    if r.status_code == 200:
        if _FORA_DO_AR.search(r.text[:200000]):
            return "fora_do_ar", "a página diz que o imóvel não está mais disponível"
        return "no_ar", "anúncio ainda publicado (saiu dos filtros ou da amostra desta busca)"
    return "nao_verificado", f"o portal respondeu {r.status_code}"


async def _comparar(anterior: tuple, imoveis: list[Imovel]) -> dict:
    """Marca novos e mudanças de preço em `imoveis` e lista o que saiu desde a busca anterior,
    verificando (até 40) se o anúncio saiu do ar."""
    historico_id, data, busca = anterior
    chave = lambda im: f"{im.fonte}:{im.id}"
    antigos = {chave(im): im for im in busca.imoveis}
    atuais = {chave(im) for im in imoveis}
    # O mesmo imóvel pode ter vindo agora por outro portal (fica em outras_fontes): compara pela URL
    urls_atuais = {im.url for im in imoveis} | {o["url"] for im in imoveis for o in im.outras_fontes}
    novos = mudou = 0
    for im in imoveis:
        velho = antigos.get(chave(im))
        if velho is None:
            im.novo = True
            novos += 1
        elif velho.total_mensal and im.total_mensal and abs(velho.total_mensal - im.total_mensal) >= 1:
            im.preco_anterior = velho.total_mensal
            mudou += 1
    saidos = [im for k, im in antigos.items() if k not in atuais and im.url not in urls_atuais]
    sem = asyncio.Semaphore(4)

    async def verificar(cliente, im):
        async with sem:
            return await _status_anuncio(cliente, im)

    async with httpx.AsyncClient(timeout=20, follow_redirects=False, headers={"User-Agent": USER_AGENT}) as cliente:
        status = await asyncio.gather(*(verificar(cliente, im) for im in saidos[:40]))
    removidos = []
    for n, im in enumerate(saidos):
        st, detalhe = status[n] if n < len(status) else ("nao_verificado", "fora do limite de verificação")
        removidos.append({"status": st, "detalhe": detalhe,
                          "imovel": im.model_dump(include={"fonte", "id", "url", "tipo", "bairro", "endereco",
                                                           "total_mensal", "aluguel", "quartos", "fotos"})})
    return {"com_historico_id": historico_id, "data_anterior": data.isoformat(), "total_anterior": busca.total,
            "novos": novos, "mudou_preco": mudou, "mantidos": len(imoveis) - novos, "removidos": removidos,
            "fora_do_ar": sum(r["status"] == "fora_do_ar" for r in removidos)}


def salvar_busca(db: Session, user: User, resposta: SearchResponse) -> SearchResponse:
    """Grava a busca (com os resultados) no histórico do usuário."""
    registro = SearchHistory(user_id=user.id, query=resposta.texto, filters_json=resposta.filtros.model_dump_json(),
                             total_results=resposta.total, results_json=resposta.model_dump_json())
    db.add(registro)
    db.commit()
    resposta.historico_id = registro.id
    resposta.data = registro.created_at
    resposta.usuario = user.username
    return resposta


async def executar_busca(req: SearchRequest, regras: list[str] | None = None,
                         anterior: tuple | None = None) -> SearchResponse:
    """Busca completa (interpretação, fontes, IA, filtros, ordenação), sem gravar nada.
    `regras`: preferências que a IA segue; `anterior`: (id, data, SearchResponse) para comparar.
    Usada pela API e pelo comando `publicar_busca.py`."""
    regras = regras or []
    filtros, erro_ia = await _interpretar(req.texto, regras)
    escolhidos = [PROVIDERS[f] for f in (req.fontes or PROVIDERS) if f in PROVIDERS]
    if not escolhidos:
        raise HTTPException(status_code=400, detail=f"Fontes válidas: {', '.join(PROVIDERS)}")

    fetcher = Fetcher()
    try:
        referencia = None
        if filtros.bairro:
            referencia = await localizar_bairro(filtros.bairro, fetcher)
            if not referencia:
                raise HTTPException(status_code=422, detail=f"Bairro '{filtros.bairro}' não encontrado em Belo Horizonte")

        async def rodar(p):
            try:
                return await p.search(filtros, fetcher, req.max_detalhes, referencia)
            except Exception as e:  # uma fonte com problema não derruba as outras
                return [], FonteStatus(fonte=p.name, ok=False, mensagem=f"Erro ao consultar: {e}")

        respostas = await asyncio.gather(*(rodar(p) for p in escolhidos))
    finally:
        await fetcher.aclose()

    imoveis, status = [], []
    for lista, st in respostas:
        imoveis.extend(lista)
        status.append(st)

    imoveis = _filtros_finais(_deduplicar(imoveis), filtros, so_dados=True)
    ia = {"ativa": llm.ativa(), "interpretacao": filtros.interpretado_por, "erros": [erro_ia] if erro_ia else [],
          "regras_usadas": len(regras) if llm.ativa() else 0}
    if llm.ativa() and req.usar_ia:
        leitura = await llm.enriquecer(imoveis, filtros)
        ia["erros"] += leitura.pop("erros")
        ia["leitura"] = leitura
    # Desejos conhecidos ("perto de farmácia"...) conferidos nas características do anúncio,
    # somando ao que a IA marcou
    for im in imoveis:
        texto = " ".join([*im.caracteristicas, *im.destaques, im.descricao or ""])
        im.atende = list(dict.fromkeys(im.atende + desejos_atendidos(texto, filtros.caracteristicas)))
    imoveis = sorted(_filtros_finais(imoveis, filtros), key=lambda im: _ordem(im, filtros))
    for st in status:
        st.encontrados = sum(im.fonte == st.fonte for im in imoveis)
    comparacao = await _comparar(anterior, imoveis) if anterior else None

    return SearchResponse(texto=req.texto, filtros=filtros, referencia=referencia, fontes=status,
                          total=len(imoveis), imoveis=imoveis, ia=ia, comparacao=comparacao)


_PAGINAS_TESTE = {
    "netimoveis": "https://www.netimoveis.com/locacao/minas-gerais/belo-horizonte/apartamento",
    "quintoandar": "https://www.quintoandar.com.br/alugar/imovel/belo-horizonte-mg-brasil",
    "chavesnamao": "https://www.chavesnamao.com.br/apartamentos-para-alugar/mg-belo-horizonte/",
    "lopes": "https://www.lopes.com.br/busca/aluguel/br/mg/belo-horizonte/tipo/apartamento",
    "casamineira": "https://www.casamineira.com.br/aluguel/apartamento/belo-horizonte_mg",
    "vivareal": "https://www.vivareal.com.br/aluguel/minas-gerais/belo-horizonte/apartamento_residencial/",
}


@router.get("/admin/diagnostico", tags=["admin"])
async def diagnostico(admin: User = Depends(require_admin)):
    """Testa, a partir deste servidor, se cada portal responde (sem cache)."""
    fetcher = Fetcher()

    async def testar(nome: str, url: str):
        t = time.time()
        try:
            html = await fetcher.get(url, use_cache=False)
            return {"fonte": nome, "ok": True, "status": 200, "segundos": round(time.time() - t, 1), "bytes": len(html)}
        except FetchError as e:
            return {"fonte": nome, "ok": False, "status": e.status, "segundos": round(time.time() - t, 1)}
        except Exception as e:  # timeout, DNS...
            return {"fonte": nome, "ok": False, "erro": type(e).__name__, "segundos": round(time.time() - t, 1)}

    try:
        fontes = await asyncio.gather(*(testar(n, u) for n, u in _PAGINAS_TESTE.items()))
    finally:
        await fetcher.aclose()
    return {"fontes": fontes, "bairros_na_tabela": len(_BAIRROS), "ia_ativa": llm.ativa(),
            "ia_provedor": llm.provedor(), "ia_modelo": llm.modelo_interpretar() if llm.ativa() else None}


@router.get("/fontes")
def fontes(user: User = Depends(get_current_user)):
    """Fontes de anúncios disponíveis para o campo `fontes` de /buscar."""
    return [{"nome": p.name, "rotulo": p.label} for p in PROVIDERS.values()]


@router.get("/interpretar", response_model=Filtros)
def interpretar(texto: str, user: User = Depends(get_current_user)):
    """Mostra como o texto foi entendido, sem executar a busca."""
    return parse_query(texto)


def _resumo(r: SearchHistory, username: str) -> HistoricoItem:
    return HistoricoItem(id=r.id, usuario=username, texto=r.query, filtros=json.loads(r.filters_json),
                         total=r.total_results, data=r.created_at, tem_resultados=r.results_json is not None,
                         tem_relatorio=r.relatorio_json is not None)


@router.get("/historico", response_model=list[HistoricoItem])
def historico(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = (db.query(SearchHistory).filter(SearchHistory.user_id == user.id)
            .order_by(SearchHistory.id.desc()).limit(200).all())
    return [_resumo(r, user.username) for r in rows]


@router.get("/admin/historico", response_model=list[HistoricoItem], tags=["admin"])
def historico_geral(usuario: str | None = None, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Buscas de todos os usuários (opcionalmente de um só)."""
    q = db.query(SearchHistory, User.username).join(User, User.id == SearchHistory.user_id)
    if usuario:
        q = q.filter(User.username == usuario)
    return [_resumo(r, username) for r, username in q.order_by(SearchHistory.id.desc()).limit(500).all()]


def _do_usuario(historico_id: int, user: User, db: Session) -> tuple[SearchHistory, User]:
    row = db.get(SearchHistory, historico_id)
    if not row or (row.user_id != user.id and not user.is_admin):
        raise HTTPException(status_code=404, detail="Busca não encontrada")
    return row, db.get(User, row.user_id)


@router.get("/historico/{historico_id}", response_model=SearchResponse)
def abrir_historico(historico_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Resultados salvos de uma busca anterior (dono da busca ou admin)."""
    row, dono = _do_usuario(historico_id, user, db)
    if not row.results_json:
        raise HTTPException(status_code=404, detail="Esta busca é anterior ao salvamento de resultados")
    resposta = SearchResponse.model_validate_json(row.results_json)
    resposta.historico_id, resposta.data, resposta.usuario = row.id, row.created_at, dono.username if dono else None
    return resposta


class RelatorioRequest(BaseModel):
    top: int = Field(default=10, ge=1, le=40)


def _resposta_relatorio(row: SearchHistory) -> dict:
    """Relatório salvo + os imóveis citados, na ordem do ranking."""
    rel = json.loads(row.relatorio_json)
    busca = SearchResponse.model_validate_json(row.results_json)
    por_id = {f"{im.fonte}:{im.id}": im for im in busca.imoveis}
    rel["imoveis"] = [por_id[r["id"]].model_dump() for r in rel["ranking"] if r["id"] in por_id]
    return rel


@router.post("/historico/{historico_id}/relatorio")
async def gerar_relatorio(historico_id: int, req: RelatorioRequest, user: User = Depends(get_current_user),
                          db: Session = Depends(get_db)):
    """A IA escolhe os `top` imóveis mais alinhados ao pedido e escreve um relatório comentado."""
    row, _ = _do_usuario(historico_id, user, db)
    if not llm.ativa():
        raise HTTPException(status_code=400, detail="IA desligada: configure OPENAI_API_KEY ou ANTHROPIC_API_KEY no servidor")
    if not row.results_json:
        raise HTTPException(status_code=404, detail="Esta busca não tem resultados salvos")
    busca = SearchResponse.model_validate_json(row.results_json)
    if not busca.imoveis:
        raise HTTPException(status_code=400, detail="A busca não encontrou imóveis para comparar")
    _checar_limite(user)
    _em_andamento.add(user.id)
    try:
        rel = await llm.relatorio(row.query, busca.filtros, busca.imoveis, req.top, regras_para(db, user))
    except (llm.ErroIA, ValueError) as e:
        raise HTTPException(status_code=502, detail=f"Não foi possível gerar o relatório: {e}")
    finally:
        _em_andamento.discard(user.id)
    rel["top"] = req.top
    rel["gerado_em"] = datetime.utcnow().isoformat()
    row.relatorio_json = json.dumps(rel, ensure_ascii=False)
    db.commit()
    return _resposta_relatorio(row)


@router.get("/historico/{historico_id}/relatorio")
def abrir_relatorio(historico_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row, _ = _do_usuario(historico_id, user, db)
    if not row.relatorio_json:
        raise HTTPException(status_code=404, detail="Ainda não há relatório para esta busca")
    return _resposta_relatorio(row)


@router.delete("/historico/{historico_id}", status_code=204)
def excluir_historico(historico_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row, _ = _do_usuario(historico_id, user, db)
    db.delete(row)
    db.commit()
