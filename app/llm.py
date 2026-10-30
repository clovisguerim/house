"""Uso de IA na busca: interpretar o texto do usuário e ler a descrição dos anúncios.

Funciona com Claude (Anthropic) ou OpenAI, conforme a chave configurada. Tudo aqui é opcional:
sem credencial (ou com HOUSE_LLM=off) a busca usa só as regras de `parser.py` e a inferência por
regex dos provedores. Qualquer erro da IA cai nesse mesmo caminho.
"""
import asyncio
import hashlib
import json
import os
import time

import anthropic
import openai

from .config import (LLM_CACHE_TTL_SECONDS, LLM_CONCORRENCIA, LLM_EFFORT, LLM_LOTE, LLM_MAX_ANUNCIOS,
                     LLM_MODELO_EXTRAIR, LLM_MODELO_INTERPRETAR, LLM_MODELOS_PADRAO, LLM_MODO, LLM_PROVEDOR)
from .db import SessionLocal
from .models import HttpCache
from .schemas import Filtros, Imovel

# Modelos Claude que aceitam o fallback do servidor quando o classificador de segurança recusa
_COM_FALLBACK = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}
TIPOS = ["apartamento", "casa", "cobertura", "area-privativa", "kitnet"]
GARANTIAS = ["seguro_fianca", "fiador", "caucao", "titulo_capitalizacao"]

_clientes: dict[str, object] = {}


class ErroIA(Exception):
    pass


def provedor() -> str | None:
    """'anthropic', 'openai' ou None (IA desligada)."""
    if LLM_MODO == "off":
        return None
    tem = {"anthropic": bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")),
           "openai": bool(os.getenv("OPENAI_API_KEY"))}
    if LLM_PROVEDOR in tem:
        return LLM_PROVEDOR if tem[LLM_PROVEDOR] or LLM_MODO == "on" else None
    return next((p for p in ("anthropic", "openai") if tem[p]), None)


def ativa() -> bool:
    return provedor() is not None


def modelo_interpretar() -> str:
    return LLM_MODELO_INTERPRETAR or LLM_MODELOS_PADRAO[provedor() or "anthropic"]


def modelo_extrair() -> str:
    return LLM_MODELO_EXTRAIR or LLM_MODELOS_PADRAO[provedor() or "anthropic"]


def _cliente(nome: str):
    if nome not in _clientes:
        _clientes[nome] = (anthropic.AsyncAnthropic(timeout=180.0, max_retries=2) if nome == "anthropic"
                           else openai.AsyncOpenAI(timeout=180.0, max_retries=2))
    return _clientes[nome]


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


async def _json(modelo: str, system: str, conteudo: str, schema: dict, max_tokens: int,
                esforco: str = LLM_EFFORT) -> dict:
    """Uma chamada com saída estruturada; devolve o JSON já validado pelo schema."""
    if provedor() == "openai":
        return await _json_openai(modelo, system, conteudo, schema, max_tokens, esforco)
    return await _json_anthropic(modelo, system, conteudo, schema, max_tokens, esforco)


async def _json_openai(modelo: str, system: str, conteudo: str, schema: dict, max_tokens: int,
                       esforco: str) -> dict:
    extra = {}
    if modelo.startswith(("gpt-5", "o")):  # modelos com raciocínio aceitam o nível de esforço
        extra["reasoning_effort"] = esforco
    try:
        resp = await _cliente("openai").chat.completions.create(
            model=modelo,
            max_completion_tokens=max_tokens,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": conteudo}],
            response_format={"type": "json_schema",
                             "json_schema": {"name": "resposta", "strict": True, "schema": schema}},
            **extra,
        )
    except openai.AuthenticationError as e:
        raise ErroIA("chave da OpenAI inválida") from e
    except openai.RateLimitError as e:
        raise ErroIA("limite de uso ou crédito da OpenAI esgotado") from e
    except openai.APIStatusError as e:
        raise ErroIA(f"API da OpenAI respondeu {e.status_code}: {e.message}") from e
    except openai.APIConnectionError as e:
        raise ErroIA("sem conexão com a API da OpenAI") from e
    escolha = resp.choices[0]
    if getattr(escolha.message, "refusal", None):
        raise ErroIA("o modelo recusou a solicitação")
    if escolha.finish_reason == "length":
        raise ErroIA("resposta do modelo cortada (limite de tokens)")
    if not escolha.message.content:
        raise ErroIA("resposta do modelo sem conteúdo")
    return json.loads(escolha.message.content)


async def _json_anthropic(modelo: str, system: str, conteudo: str, schema: dict, max_tokens: int,
                          esforco: str) -> dict:
    extra = {}
    if modelo in _COM_FALLBACK:
        extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    try:
        resp = await _cliente("anthropic").beta.messages.create(
            model=modelo,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": conteudo}],
            output_config={"effort": esforco, "format": {"type": "json_schema", "schema": schema}},
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
    chave = _chave("interpretar", modelo_interpretar(), texto.strip().lower())
    dados = _cache_get(chave)
    if dados is None:
        dados = await _json(modelo_interpretar(), _SYSTEM_INTERPRETAR, texto, _SCHEMA_FILTROS, 4000)
        _cache_set(chave, dados)
    if not dados.get("tipos"):
        dados["tipos"] = ["apartamento", "casa"]
    return Filtros(**dados, interpretado_por=modelo_interpretar())


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
    return _chave("extrair", modelo_extrair(), im.fonte, im.id, im.descricao or "", "|".join(sorted(desejos)))


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
                resp = await _json(modelo_extrair(), _SYSTEM_EXTRAIR, conteudo, _SCHEMA_ANUNCIOS, 16000)
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
        "modelo": modelo_extrair(),
        "anuncios_lidos": do_cache + lidos_agora,
        "do_cache": do_cache,
        "novos": lidos_agora,
        "sem_descricao": len(imoveis) - len(com_texto),
        "fora_do_limite": max(0, len(com_texto) - LLM_MAX_ANUNCIOS),
        "erros": sorted(set(erros)),
    }


# --- 3. Relatório com os N imóveis mais alinhados à busca ---------------------------------------

_SCHEMA_RELATORIO = {
    "type": "object",
    "additionalProperties": False,
    "required": ["resumo", "ranking", "comparativo", "proximos_passos"],
    "properties": {
        "resumo": {"type": "string"},
        "ranking": {"type": "array", "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["id", "nota", "titulo_curto", "por_que", "pontos_fortes", "pontos_de_atencao",
                         "perguntas_para_o_anunciante"],
            "properties": {
                "id": {"type": "string"},
                "nota": {"type": "integer"},
                "titulo_curto": {"type": "string"},
                "por_que": {"type": "string"},
                "pontos_fortes": {"type": "array", "items": {"type": "string"}},
                "pontos_de_atencao": {"type": "array", "items": {"type": "string"}},
                "perguntas_para_o_anunciante": {"type": "array", "items": {"type": "string"}},
            },
        }},
        "comparativo": {"type": "string"},
        "proximos_passos": {"type": "array", "items": {"type": "string"}},
    },
}

_SYSTEM_RELATORIO = """Você é um consultor imobiliário ajudando alguém a escolher um imóvel para ALUGAR em \
Belo Horizonte. Receberá o pedido original da pessoa, os filtros entendidos e uma lista de candidatos já \
filtrados. Escolha os N candidatos MAIS ALINHADOS ao pedido e escreva um relatório em português do Brasil.

Regras:
- Use SOMENTE os dados fornecidos. Nunca invente informação; quando um dado importante não estiver no \
anúncio (pet, garantias, vaga, condomínio...), diga que não foi informado e transforme em pergunta para o anunciante.
- Ordene do mais para o menos alinhado. Pese primeiro o que a pessoa pediu explicitamente (tipo, preço/total, \
distância, quartos, vaga, pet, garantias, desejos), depois custo-benefício.
- nota: 0 a 10, coerente com a ordem.
- id: copie exatamente o id do candidato.
- titulo_curto: tipo + bairro + o diferencial, em poucas palavras.
- por_que: 1–2 frases objetivas citando os números (total mensal, distância, quartos...).
- pontos_fortes / pontos_de_atencao: até 4 itens curtos cada.
- perguntas_para_o_anunciante: até 3 perguntas práticas sobre o que falta saber.
- resumo: 2–3 frases sobre o que a busca encontrou e o melhor achado.
- comparativo: um parágrafo comparando os escolhidos (preço x localização x espaço).
- proximos_passos: até 4 ações concretas (ex.: visitar primeiro X, confirmar Y)."""


def _candidato(im: Imovel) -> dict:
    """Só os campos úteis para a decisão, para caber muitos candidatos numa chamada."""
    d = im.model_dump(include={"tipo", "bairro", "endereco", "distancia_km", "quartos", "banheiros", "area_m2",
                               "vagas", "andar", "aluguel", "condominio", "iptu", "total_mensal", "aceita_pet",
                               "seguro_fianca", "fiador", "caucao", "titulo_capitalizacao", "mobiliado",
                               "destaques", "atende"})
    d = {k: v for k, v in d.items() if v not in (None, [], "")}
    d["id"] = f"{im.fonte}:{im.id}"
    if im.caracteristicas:
        d["caracteristicas"] = im.caracteristicas[:15]
    return d


async def relatorio(texto: str, filtros: Filtros, imoveis: list[Imovel], top: int, max_candidatos: int = 40) -> dict:
    """Ranking comentado dos `top` imóveis mais alinhados ao pedido. Os candidatos já vêm na ordem do app."""
    candidatos = imoveis[:max_candidatos]
    conteudo = json.dumps({
        "pedido": texto,
        "filtros_entendidos": filtros.model_dump(exclude_defaults=True, exclude={"interpretado_por"}),
        "N": min(top, len(candidatos)),
        "candidatos": [_candidato(im) for im in candidatos],
    }, ensure_ascii=False)
    dados = await _json(modelo_interpretar(), _SYSTEM_RELATORIO, conteudo, _SCHEMA_RELATORIO, 16000, esforco="medium")
    validos = {f"{im.fonte}:{im.id}" for im in candidatos}
    vistos = set()
    dados["ranking"] = [r for r in dados["ranking"] if r["id"] in validos and not (r["id"] in vistos or vistos.add(r["id"]))][:top]
    for r in dados["ranking"]:
        r["nota"] = max(0, min(10, r["nota"]))
    dados["modelo"] = modelo_interpretar()
    dados["candidatos_avaliados"] = len(candidatos)
    return dados
