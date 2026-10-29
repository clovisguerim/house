"""Uso do Claude na busca: interpretar o texto do usuário e ler a descrição dos anúncios.

Tudo aqui é opcional. Sem credencial (ou com HOUSE_LLM=off) a busca usa só as regras de
`parser.py` e a inferência por regex dos provedores. Qualquer erro da IA cai nesse mesmo caminho.
"""
import asyncio
import hashlib
import json
import os
import time

import anthropic

from .config import (LLM_CACHE_TTL_SECONDS, LLM_CONCORRENCIA, LLM_EFFORT, LLM_LOTE, LLM_MAX_ANUNCIOS,
                     LLM_MODELO_EXTRAIR, LLM_MODELO_INTERPRETAR, LLM_MODO)
from .db import SessionLocal
from .models import HttpCache
from .schemas import Filtros, Imovel

# Modelos que aceitam o fallback do servidor quando o classificador de segurança recusa
_COM_FALLBACK = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}
TIPOS = ["apartamento", "casa", "cobertura", "area-privativa", "kitnet"]
GARANTIAS = ["seguro_fianca", "fiador", "caucao", "titulo_capitalizacao"]

_client: anthropic.AsyncAnthropic | None = None


class ErroIA(Exception):
    pass


def ativa() -> bool:
    if LLM_MODO == "off":
        return False
    return LLM_MODO == "on" or bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))


def _cliente() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(timeout=180.0, max_retries=2)
    return _client


# --- cache em SQLite (mesma tabela do cache HTTP, com chave própria) -----------------------

def _chave(*partes: str) -> str:
    return "llm:" + hashlib.sha256("\x1f".join(partes).encode()).hexdigest()


def _cache_get(chave: str) -> dict | None:
    with SessionLocal() as db:
        row = db.get(HttpCache, chave)
        if row and time.time() - row.fetched_at < LLM_CACHE_TTL_SECONDS:
            return json.loads(row.body)
    return None


def _cache_set(chave: str, valor: dict) -> None:
    with SessionLocal() as db:
        db.merge(HttpCache(url=chave, body=json.dumps(valor, ensure_ascii=False), fetched_at=time.time()))
        db.commit()


async def _json(modelo: str, system: str, conteudo: str, schema: dict, max_tokens: int) -> dict:
    """Uma chamada com saída estruturada; devolve o JSON já validado pelo schema."""
    extra = {}
    if modelo in _COM_FALLBACK:
        extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    try:
        resp = await _cliente().beta.messages.create(
            model=modelo,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": conteudo}],
            output_config={"effort": LLM_EFFORT, "format": {"type": "json_schema", "schema": schema}},
            **extra,
        )
    except anthropic.AuthenticationError as e:
        raise ErroIA("credencial da Anthropic inválida") from e
    except anthropic.RateLimitError as e:
        raise ErroIA("limite de uso da API da Anthropic atingido") from e
    except anthropic.APIStatusError as e:
        raise ErroIA(f"API da Anthropic respondeu {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise ErroIA("sem conexão com a API da Anthropic") from e
    if resp.stop_reason == "refusal":
        raise ErroIA("o modelo recusou a solicitação")
    if resp.stop_reason == "max_tokens":
        raise ErroIA("resposta do modelo cortada (max_tokens)")
    texto = next((b.text for b in resp.content if b.type == "text"), None)
    if not texto:
        raise ErroIA("resposta do modelo sem conteúdo")
    return json.loads(texto)


def _anulavel(tipo: str) -> dict:
    return {"anyOf": [{"type": tipo}, {"type": "null"}]}


# --- 1. Interpretar a pergunta ----------------------------------------------------------------

_SCHEMA_FILTROS = {
    "type": "object",
    "additionalProperties": False,
    "required": ["tipos", "quartos_min", "quartos_max", "bairro", "raio_km", "preco_min", "preco_max", "total_max",
                 "vagas_min", "aceita_pet", "garantias", "andar", "andares_excluidos", "caracteristicas"],
    "properties": {
        "tipos": {"type": "array", "items": {"type": "string", "enum": TIPOS}},
        "quartos_min": _anulavel("integer"),
        "quartos_max": _anulavel("integer"),
        "bairro": _anulavel("string"),
        "raio_km": _anulavel("number"),
        "preco_min": _anulavel("number"),
        "preco_max": _anulavel("number"),
        "total_max": _anulavel("number"),
        "vagas_min": _anulavel("integer"),
        "aceita_pet": _anulavel("boolean"),
        "garantias": {"type": "array", "items": {"type": "string", "enum": GARANTIAS}},
        "andar": _anulavel("integer"),
        "andares_excluidos": {"type": "array", "items": {"type": "integer"}},
        "caracteristicas": {"type": "array", "items": {"type": "string"}},
    },
}

_SYSTEM_INTERPRETAR = """Você converte o pedido de alguém que procura imóvel para ALUGAR em Belo Horizonte \
em filtros estruturados. Preencha só o que o pedido diz; o que não for mencionado fica null ou lista vazia.

- tipos: subconjunto de apartamento, casa, cobertura, area-privativa, kitnet. "Área privativa" em BH é \
apartamento (geralmente térreo) com área externa privativa. "Casa ou apartamento com área privativa" = \
[casa, area-privativa]. "Apartamento" sozinho já inclui coberturas e áreas privativas. Sem tipo citado: \
[apartamento, casa].
- quartos_min/quartos_max: "3 quartos" significa 3 ou mais (quartos_min=3). "Exatamente 3" usa os dois.
- bairro: o bairro de referência como o usuário escreveu (um só; se citar vários, o principal). \
raio_km: distância máxima em km a partir dele ("perto" sem número: deixe null).
- preco_max/preco_min: valor do ALUGUEL. total_max: limite de aluguel + condomínio, quando o pedido fala em \
total, "com condomínio", "tudo incluído" ou similar. "3 mil" = 3000.
- vagas_min: vaga/garagem/estacionamento pedidos => pelo menos 1 (ou o número dito). "Sem garagem" => null.
- aceita_pet: true só se o pedido exige que aceite animais.
- garantias: garantias locatícias que o usuário quer poder usar (seguro_fianca, fiador, caucao, \
titulo_capitalizacao).
- andar: andar pedido para apartamento (0 = térreo). andares_excluidos: andares que o usuário não quer.
- caracteristicas: desejos que não cabem nos campos acima, em frases curtas em português \
(ex.: "quintal", "mobiliado", "perto do metrô", "varanda", "silencioso"). No máximo 6."""


async def interpretar(texto: str) -> Filtros:
    chave = _chave("interpretar", LLM_MODELO_INTERPRETAR, texto.strip().lower())
    dados = _cache_get(chave)
    if dados is None:
        dados = await _json(LLM_MODELO_INTERPRETAR, _SYSTEM_INTERPRETAR, texto, _SCHEMA_FILTROS, 4000)
        _cache_set(chave, dados)
    if not dados.get("tipos"):
        dados["tipos"] = ["apartamento", "casa"]
    return Filtros(**dados, interpretado_por=LLM_MODELO_INTERPRETAR)


# --- 2. Ler a descrição dos anúncios ------------------------------------------------------------

_SCHEMA_ANUNCIOS = {
    "type": "object",
    "additionalProperties": False,
    "required": ["imoveis"],
    "properties": {"imoveis": {"type": "array", "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "aceita_pet", *GARANTIAS, "andar", "mobiliado", "destaques", "atende"],
        "properties": {
            "id": {"type": "string"},
            "aceita_pet": _anulavel("boolean"),
            **{g: _anulavel("boolean") for g in GARANTIAS},
            "andar": _anulavel("integer"),
            "mobiliado": _anulavel("boolean"),
            "destaques": {"type": "array", "items": {"type": "string"}},
            "atende": {"type": "array", "items": {"type": "string"}},
        },
    }}},
}

_SYSTEM_EXTRAIR = """Você lê anúncios de imóveis para alugar e extrai fatos do TEXTO do anúncio. \
Responda uma entrada por anúncio, com o mesmo id. Use null sempre que o anúncio não disser nada; nunca chute.

- aceita_pet: true se diz que aceita/permite animais; false se proíbe ou restringe.
- seguro_fianca, fiador, caucao, titulo_capitalizacao: true se a garantia é aceita. Se o anúncio lista as \
garantias aceitas, as que ficaram fora da lista são false. "Sem fiador"/"dispensa fiador" é propaganda de \
que não é preciso fiador; sozinho não diz se fiador é aceito (null).
- andar: andar da unidade se o texto disser (0 = térreo). "Prédio de 4 andares" ou "2 apartamentos por \
andar" não é o andar da unidade.
- mobiliado: true se mobiliado/semimobiliado, false se diz que não é.
- destaques: até 4 pontos fortes concretos e curtos, em português (ex.: "quintal de 40 m²", "reformado", \
"sol da manhã"). Não repita preço, quartos, vagas ou área.
- atende: quais itens da lista DESEJOS o anúncio claramente atende, copiando o texto exato do item."""


def _chave_anuncio(im: Imovel, desejos: list[str]) -> str:
    return _chave("extrair", LLM_MODELO_EXTRAIR, im.fonte, im.id, im.descricao or "", "|".join(sorted(desejos)))


def _texto_anuncio(im: Imovel) -> dict:
    return {
        "id": f"{im.fonte}:{im.id}",
        "tipo": im.tipo,
        "titulo": im.titulo,
        "caracteristicas": im.caracteristicas[:40],
        "descricao": (im.descricao or "")[:3000],
    }


def _aplicar(im: Imovel, dados: dict, desejos: list[str]) -> None:
    """A IA só preenche o que ainda está vazio: dado estruturado do portal tem prioridade."""
    for campo in ("aceita_pet", *GARANTIAS, "andar", "mobiliado"):
        if getattr(im, campo) is None and dados.get(campo) is not None:
            setattr(im, campo, dados[campo])
    im.destaques = [d for d in dados.get("destaques", []) if d][:4]
    im.atende = [a for a in dados.get("atende", []) if a in desejos]


async def enriquecer(imoveis: list[Imovel], filtros: Filtros) -> dict:
    """Lê a descrição dos anúncios (os mais próximos primeiro) e completa os campos.

    Devolve estatísticas para a resposta da busca. Erros em um lote não afetam os outros."""
    desejos = filtros.caracteristicas
    com_texto = [im for im in imoveis if im.descricao]
    com_texto.sort(key=lambda im: im.distancia_km if im.distancia_km is not None else 999)
    alvo = com_texto[:LLM_MAX_ANUNCIOS]

    pendentes, do_cache = [], 0
    for im in alvo:
        if (dados := _cache_get(_chave_anuncio(im, desejos))) is not None:
            _aplicar(im, dados, desejos)
            do_cache += 1
        else:
            pendentes.append(im)

    sem = asyncio.Semaphore(LLM_CONCORRENCIA)
    erros: list[str] = []
    lidos_agora = 0

    async def lote(grupo: list[Imovel]):
        nonlocal lidos_agora
        conteudo = json.dumps({"desejos": desejos, "anuncios": [_texto_anuncio(im) for im in grupo]},
                              ensure_ascii=False)
        async with sem:
            try:
                resp = await _json(LLM_MODELO_EXTRAIR, _SYSTEM_EXTRAIR, conteudo, _SCHEMA_ANUNCIOS, 16000)
            except (ErroIA, json.JSONDecodeError) as e:
                erros.append(str(e))
                return
        por_id = {d["id"]: d for d in resp.get("imoveis", [])}
        for im in grupo:
            if (dados := por_id.get(f"{im.fonte}:{im.id}")) is not None:
                _cache_set(_chave_anuncio(im, desejos), dados)
                _aplicar(im, dados, desejos)
                lidos_agora += 1

    await asyncio.gather(*(lote(pendentes[i:i + LLM_LOTE]) for i in range(0, len(pendentes), LLM_LOTE)))
    return {
        "modelo": LLM_MODELO_EXTRAIR,
        "anuncios_lidos": do_cache + lidos_agora,
        "do_cache": do_cache,
        "novos": lidos_agora,
        "sem_descricao": len(imoveis) - len(com_texto),
        "fora_do_limite": max(0, len(com_texto) - LLM_MAX_ANUNCIOS),
        "erros": sorted(set(erros)),
    }
