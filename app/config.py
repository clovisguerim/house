import os
import secrets
from pathlib import Path


def _secret_key() -> str:
    """HOUSE_SECRET_KEY, ou uma chave aleatória gerada uma vez e guardada em .secret_key."""
    if key := os.getenv("HOUSE_SECRET_KEY"):
        return key
    arquivo = Path(__file__).resolve().parent.parent / ".secret_key"
    if not arquivo.exists():
        arquivo.write_text(secrets.token_urlsafe(48), encoding="utf-8")
    return arquivo.read_text(encoding="utf-8").strip()


SECRET_KEY = _secret_key()
ALGORITHM = "HS256"
TOKEN_EXPIRE_MINUTES = int(os.getenv("HOUSE_TOKEN_EXPIRE_MINUTES", "720"))


def normalizar_url_db(url: str) -> str:
    """Render/Neon/Supabase entregam postgres:// ; o SQLAlchemy usa o driver psycopg 3."""
    for prefixo in ("postgres://", "postgresql://"):
        if url.startswith(prefixo):
            return "postgresql+psycopg://" + url[len(prefixo):]
    return url


# HOUSE_DATABASE_URL, ou DATABASE_URL (padrão dos hosts gratuitos), ou SQLite local
DATABASE_URL = normalizar_url_db(os.getenv("HOUSE_DATABASE_URL") or os.getenv("DATABASE_URL") or "sqlite:///./house.db")

# Contas administradoras criadas na primeira inicialização. A senha inicial precisa ser trocada
# no primeiro acesso. Em servidor público, defina HOUSE_SENHA_INICIAL_ADMIN (o render.yaml gera uma).
_SENHA_INICIAL = os.getenv("HOUSE_SENHA_INICIAL_ADMIN", "123456")
ADMIN_SEED = [("clovisguerim", _SENHA_INICIAL), ("manucfe", _SENHA_INICIAL)]

# Cadastro de novos usuários: "aprovacao" (conta nasce inativa até um admin aprovar),
# "aberto" (entra direto) ou "fechado" (só admin cria contas)
CADASTRO = os.getenv("HOUSE_CADASTRO", "aprovacao")

# Buscas por hora para usuários comuns (cada busca abre centenas de páginas e pode usar a IA)
BUSCAS_POR_HORA = int(os.getenv("HOUSE_BUSCAS_POR_HORA", "20"))

# IA. Liga sozinha quando há ANTHROPIC_API_KEY (Claude) ou OPENAI_API_KEY (OpenAI); HOUSE_LLM=off desliga.
# Com as duas chaves, HOUSE_LLM_PROVEDOR escolhe ("anthropic" ou "openai"); em "auto" vale a Anthropic.
LLM_MODO = os.getenv("HOUSE_LLM", "auto")
LLM_PROVEDOR = os.getenv("HOUSE_LLM_PROVEDOR", "auto")
LLM_MODELOS_PADRAO = {"anthropic": "claude-opus-5-5", "openai": "gpt-5.4-mini"}
LLM_MODELO_INTERPRETAR = os.getenv("HOUSE_LLM_MODELO_INTERPRETAR")  # vazio = padrão do provedor
LLM_MODELO_EXTRAIR = os.getenv("HOUSE_LLM_MODELO_EXTRAIR")
LLM_EFFORT = os.getenv("HOUSE_LLM_EFFORT", "low")  # extração/classificação: low costuma bastar
LLM_LOTE = int(os.getenv("HOUSE_LLM_LOTE", "12"))  # anúncios por chamada
LLM_CONCORRENCIA = int(os.getenv("HOUSE_LLM_CONCORRENCIA", "4"))
LLM_MAX_ANUNCIOS = int(os.getenv("HOUSE_LLM_MAX_ANUNCIOS", "300"))  # por busca, os mais próximos primeiro
LLM_CACHE_TTL_SECONDS = 30 * 24 * 3600

# Scraping
USER_AGENT = "HouseSearch/1.0 (busca pessoal de imoveis para alugar)"
HTTP_TIMEOUT = 30
MAX_CONCURRENCY = 4
CACHE_TTL_SECONDS = 12 * 3600
