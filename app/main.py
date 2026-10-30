from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect, text

from .config import ADMIN_SEED
from .db import Base, SessionLocal, engine
from .models import User
from .routers import auth, regras, search, users
from .security import hash_password, verify_password

STATIC = Path(__file__).parent / "static"


def migrate():
    """Colunas novas em bancos criados por versões anteriores (create_all não altera tabelas)."""
    novas = [("search_history", "results_json", "TEXT"),
             ("search_history", "relatorio_json", "TEXT"),
             ("users", "must_change_password", "BOOLEAN NOT NULL DEFAULT FALSE")]
    for tabela, coluna, tipo in novas:
        if coluna not in {c["name"] for c in inspect(engine).get_columns(tabela)}:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {tipo}"))


def seed_admins():
    with SessionLocal() as db:
        for username, password in ADMIN_SEED:
            user = db.query(User).filter(User.username == username).first()
            if not user:
                user = User(username=username, hashed_password=hash_password(password), is_admin=True)
                db.add(user)
            user.is_admin = True
            # Enquanto a senha for a inicial, o acesso fica limitado à troca de senha
            if verify_password(password, user.hashed_password):
                user.must_change_password = True
        db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    migrate()
    seed_admins()
    yield


app = FastAPI(
    title="House - Busca de imóveis para alugar em BH",
    description="Busca casas e apartamentos para alugar em Belo Horizonte a partir de texto livre.",
    version="1.1.0",
    lifespan=lifespan,
)
@app.middleware("http")
async def sem_cache_velho(request: Request, call_next):
    """Página e arquivos da interface: o navegador sempre confere se há versão nova (ETag/304).
    Sem isso, depois de um deploy o navegador seguia usando o app.js antigo do cache."""
    resposta = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        resposta.headers["Cache-Control"] = "no-cache"
    return resposta


app.include_router(auth.router)
app.include_router(users.router)
app.include_router(search.router)
app.include_router(regras.router)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
def frontend():
    return FileResponse(STATIC / "index.html")


@app.get("/health", tags=["status"])
def health():
    return {"status": "ok", "docs": "/docs"}
