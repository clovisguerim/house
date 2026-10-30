"""Uso de IA na busca: interpretar o texto do usuário e ler a descrição dos anúncios.

Funciona com Claude (Anthropic) ou OpenAI, conforme a chave configurada. Tudo aqui é opcional:
sem credencial (ou com HOUSE_LLM=off) a busca usa só as regras de `parser.py` e a inferência por
regex dos provedores. Qualquer erro da IA cai nesse mesmo caminho.
"""
import asyncio
import hashlib
import json
import os
import re
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


def bloco_regras(regras: list[str]) -> str:
    """Regras de preferência acrescentadas às instruções da IA (vazio se não houver)."""
    if not regras:
        return ""
    itens = "\n".join(f"- {r}" for r in regras)
    return ("\n\nREGRAS DE PREFERÊNCIA dos usuários (aprendidas com avaliações anteriores). Siga-as sempre que "
            "se aplicarem ao pedido; se alguma contradisser o que o pedido diz explicitamente, vale o pedido:\n" + itens)


async def interpretar(texto: str, regras: list[str] | None = None) -> Filtros:
    regras = regras or []
    chave = _chave("interpretar", modelo_interpretar(), texto.strip().lower(), *regras)
    dados = _cache_get(chave)
    if dados is None:
        dados = await _json(modelo_interpretar(), _SYSTEM_INTERPRETAR + bloco_regras(regras), texto,
                            _SCHEMA_FILTROS, 4000)
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

_SYSTEM_RELATORIO = """Você é um consultor imobiliário ajudando alguém a escolher um imóvel para ALUGAR em Belo Horizonte. Receberá o pedido original da pessoa, os filtros entendidos e uma lista de candidatos já filtrados. Escolha EXATAMENTE N candidatos (ou todos, se houver menos de N) — os MAIS ALINHADOS ao pedido — e escreva um relatório em português do Brasil.

Regras:
- Use SOMENTE os dados fornecidos. Nunca invente informação; quando um dado importante não estiver no anúncio (pet, garantias, vaga, condomínio...), diga que não foi informado e transforme em pergunta para o anunciante.
- Ordene do mais para o menos alinhado. Pese primeiro o que a pessoa pediu explicitamente (tipo, preço/total, distância, quartos, vaga, pet, garantias, desejos), depois custo-benefício.
- Se vier a lista "ja_escolhidos", eles já ocupam as primeiras posições: escolha os PRÓXIMOS, sem repetir, com notas menores ou iguais à última nota já dada.
- nota: 0 a 10, coerente com a ordem.
- id: copie exatamente o código do candidato (ex.: "c7"). Use esse código SÓ no campo id: nos textos, nunca escreva códigos como "c7" — fale do imóvel pelo tipo e bairro.
- titulo_curto: tipo + bairro + o diferencial, em poucas palavras.
- por_que: 1–2 frases objetivas citando os números (total mensal, distância, quartos...).
- pontos_fortes / pontos_de_atencao: até 4 itens curtos cada.
- perguntas_para_o_anunciante: até 3 perguntas práticas sobre o que falta saber.
- resumo: 2–3 frases sobre o que a busca encontrou e o melhor achado.
- comparativo: um parágrafo comparando os escolhidos (preço x localização x espaço).
- proximos_passos: até 4 ações concretas (ex.: visitar primeiro X, confirmar Y)."""

# Itens por chamada: listas longas numa chamada só voltam incompletas ou cortadas
_LOTE_RELATORIO = 15


def _candidato(codigo: str, im: Imovel) -> dict:
    """Só os campos úteis para a decisão, para caber muitos candidatos numa chamada."""
    d = im.model_dump(include={"tipo", "bairro", "endereco", "distancia_km", "quartos", "banheiros", "area_m2",
                               "vagas", "andar", "aluguel", "condominio", "iptu", "total_mensal", "aceita_pet",
                               "seguro_fianca", "fiador", "caucao", "titulo_capitalizacao", "mobiliado",
                               "destaques", "atende"})
    d = {k: v for k, v in d.items() if v not in (None, [], "")}
    d["id"] = codigo  # código curto (c1, c2...): o modelo erra menos ao copiar do que com o id do portal
    if im.caracteristicas:
        d["caracteristicas"] = im.caracteristicas[:15]
    if im.onibus:
        d["onibus_500m"] = {"linhas": len(im.onibus["linhas"]), "ponto_mais_proximo_m": im.onibus["ponto_mais_proximo_m"],
                            "exemplos": [f"{l['numero']} {l['nome']}" for l in im.onibus["linhas"][:5]]}
    return d


_SCHEMA_SINTESE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["resumo", "comparativo", "proximos_passos"],
    "properties": {
        "resumo": {"type": "string"},
        "comparativo": {"type": "string"},
        "proximos_passos": {"type": "array", "items": {"type": "string"}},
    },
}

_SYSTEM_SINTESE = """Você é um consultor imobiliário. Recebe o pedido de quem procura imóvel para alugar em Belo Horizonte e o ranking final já pronto. Escreva em português do Brasil:
- resumo: 2–3 frases sobre o que a busca encontrou e o melhor achado;
- comparativo: um parágrafo comparando os primeiros colocados (preço x localização x espaço);
- proximos_passos: até 4 ações concretas.
Refira-se aos imóveis SEMPRE pela posição e pelo título (ex.: "o 1º, área privativa no Glória"). Use só os dados fornecidos."""

_CODIGO = re.compile(r"\b[cC]\s?(\d{1,3})\b")


# Código citado num texto, com a palavra anterior (artigo/preposição) para não repetir "o o 1º"
_CODIGO_NO_TEXTO = re.compile(r"\b(?:(o|a|do|da|no|na|ao|pelo|pela|com o|com a)\s+)?[cC]\s?(\d{1,3})\b", re.IGNORECASE)


def _codigo_valido(bruto: str, validos: dict) -> str | None:
    """Aceita "c7", "C7", "c 7" ou "7" como o código c7."""
    m = _CODIGO.fullmatch(bruto.strip()) or re.fullmatch(r"(\d{1,3})", bruto.strip())
    return f"c{m.group(1)}" if m and f"c{m.group(1)}" in validos else None


async def relatorio(texto: str, filtros: Filtros, imoveis: list[Imovel], top: int,
                    regras: list[str] | None = None) -> dict:
    """Ranking comentado dos `top` imóveis mais alinhados ao pedido. Os candidatos já vêm na ordem do app.

    1. Ranking em lotes (listas longas numa chamada só voltam incompletas). Lote que não acrescenta
       nada é repetido menor; o que ainda faltar é completado pela ordem do app, sem comentário da IA.
    2. Síntese (resumo, comparativo, próximos passos) escrita sobre o ranking final, por posição.
    Códigos internos (c7) que escaparem nos textos viram "o Nº (título)"."""
    candidatos = imoveis[:max(40, top + 20)]
    top = min(top, len(candidatos))
    codigos = {f"c{k}": im for k, im in enumerate(candidatos, 1)}
    filtros_json = filtros.model_dump(exclude_defaults=True, exclude={"interpretado_por"})

    ranking: list[dict] = []
    lote = _LOTE_RELATORIO
    for _ in range(top // _LOTE_RELATORIO + 5):
        faltam = top - len(ranking)
        if faltam <= 0:
            break
        escolhidos = {r["id"] for r in ranking}
        restantes = {c: im for c, im in codigos.items() if c not in escolhidos}
        pedido = {"pedido": texto, "filtros_entendidos": filtros_json, "N": min(faltam, lote),
                  "candidatos": [_candidato(c, im) for c, im in restantes.items()]}
        if ranking:
            pedido["ja_escolhidos"] = [{"titulo_curto": r["titulo_curto"], "nota": r["nota"]} for r in ranking]
        dados = await _json(modelo_interpretar(), _SYSTEM_RELATORIO + bloco_regras(regras or []),
                            json.dumps(pedido, ensure_ascii=False),
                            _SCHEMA_RELATORIO, 16000, esforco="medium")
        novos = 0
        for r in dados["ranking"]:
            codigo = _codigo_valido(r["id"], restantes)
            if codigo and codigo not in {x["id"] for x in ranking} and len(ranking) < top:
                r["id"] = codigo
                r["nota"] = max(0, min(10, r["nota"]))
                ranking.append(r)
                novos += 1
        if novos == 0:
            lote = max(5, lote // 2)  # tenta de novo pedindo menos de uma vez

    # Ainda faltando: completa com os próximos da ordem do app (que já prioriza o pedido)
    ultima_nota = ranking[-1]["nota"] if ranking else 5
    for c, im in codigos.items():
        if len(ranking) >= top:
            break
        if c in {r["id"] for r in ranking}:
            continue
        ranking.append({"id": c, "nota": ultima_nota, "sem_ia": True,
                        "titulo_curto": f"{(im.tipo or 'imóvel').replace('-', ' ').capitalize()} no {im.bairro or 'bairro não informado'}",
                        "por_que": "Incluído pela ordem do app (prioridades e distância); a IA não comentou este imóvel.",
                        "pontos_fortes": [], "pontos_de_atencao": [], "perguntas_para_o_anunciante": []})

    # Síntese sobre o ranking final, citando posições
    resumo_ranking = [{"posicao": n, "titulo": r["titulo_curto"], "nota": r["nota"],
                       **{k: v for k, v in _candidato(r["id"], codigos[r["id"]]).items() if k != "id"}}
                      for n, r in enumerate(ranking[:15], 1)]
    try:
        textos = await _json(modelo_interpretar(), _SYSTEM_SINTESE + bloco_regras(regras or []),
                             json.dumps({"pedido": texto, "ranking": resumo_ranking, "total_no_ranking": len(ranking)},
                                        ensure_ascii=False), _SCHEMA_SINTESE, 8000, esforco="medium")
    except (ErroIA, ValueError):
        textos = {"resumo": "", "comparativo": "", "proximos_passos": []}

    # Troca códigos que escaparam ("o c20") por posição + título, ou tipo + bairro se ficou de fora
    posicao = {r["id"]: (n, r["titulo_curto"]) for n, r in enumerate(ranking, 1)}

    def legivel(t: str) -> str:
        def sub(m):
            artigo, c = m.group(1), f"c{m.group(2)}"  # "o c1" -> "o 1º"; "do c3" -> "do 3º"
            prefixo = f"{artigo} " if artigo else "o "
            if c in posicao:
                return f"{prefixo}{posicao[c][0]}º ({posicao[c][1]})"
            im = codigos.get(c)
            if not im:
                return m.group(0)
            return f"{prefixo}imóvel ({(im.tipo or 'imóvel').replace('-', ' ')} no {im.bairro})"
        return _CODIGO_NO_TEXTO.sub(sub, t)

    for r in ranking:
        for campo in ("titulo_curto", "por_que"):
            r[campo] = legivel(r[campo])
        for campo in ("pontos_fortes", "pontos_de_atencao", "perguntas_para_o_anunciante"):
            r[campo] = [legivel(x) for x in r[campo]]
    textos = {"resumo": legivel(textos["resumo"]), "comparativo": legivel(textos["comparativo"]),
              "proximos_passos": [legivel(x) for x in textos["proximos_passos"]]}

    for r in ranking:  # volta para o id real do anúncio
        im = codigos[r["id"]]
        r["id"] = f"{im.fonte}:{im.id}"
    return {**textos, "ranking": ranking, "modelo": modelo_interpretar(), "candidatos_avaliados": len(candidatos),
            "comentados_pela_ia": sum(not r.get("sem_ia") for r in ranking), "regras_usadas": len(regras or [])}


# --- 4. Transformar um feedback em regra --------------------------------------------------------

_SCHEMA_REGRA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["regra"],
    "properties": {"regra": {"type": "string"}},
}

_SYSTEM_REGRA = """Uma pessoa avaliou a análise que uma IA fez de imóveis para alugar em Belo Horizonte. \
Transforme a avaliação numa REGRA DE PREFERÊNCIA curta (no máximo 200 caracteres), em português, \
escrita como instrução para a IA em buscas futuras.

- Generalize: nada de citar um anúncio, endereço ou id específico; descreva o critério (ex.: "Não trate \
casa geminada como casa independente", "Condomínio acima de R$ 500 deve pesar contra", "Valorize mais 2 \
vagas de garagem do que área maior").
- Nota baixa costuma indicar um erro da IA a evitar; nota alta, um acerto a manter.
- Se a avaliação não permitir uma regra geral, resuma a justificativa como orientação."""


async def regra_de_feedback(pedido: str, nota: int, justificativa: str, imovel: dict | None,
                            avaliacao_ia: dict | None) -> str:
    """Texto da regra proposta a partir de um feedback (a pessoa pode editar depois)."""
    contexto = {"pedido_da_busca": pedido, "nota_dada_1_a_5": nota, "justificativa": justificativa}
    if imovel:
        contexto["imovel_avaliado"] = {k: imovel.get(k) for k in ("tipo", "bairro", "distancia_km", "quartos",
                                                                   "vagas", "total_mensal", "aceita_pet")}
    if avaliacao_ia:
        contexto["o_que_a_ia_disse"] = {k: avaliacao_ia.get(k) for k in ("nota", "titulo_curto", "por_que")}
    dados = await _json(modelo_interpretar(), _SYSTEM_REGRA, json.dumps(contexto, ensure_ascii=False),
                        _SCHEMA_REGRA, 2000)
    return dados["regra"].strip()[:500]
