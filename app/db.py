from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import DATABASE_URL

SQLITE = DATABASE_URL.startswith("sqlite")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if SQLITE else {},
    pool_pre_ping=not SQLITE,  # Postgres gratuito derruba conexões ociosas
)

if SQLITE:
    @event.listens_for(engine, "connect")
    def _sqlite_fk(dbapi_conn, _):
        # SQLite só respeita ON DELETE CASCADE com foreign_keys ligado
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

SessionLocal = sessionmaker(bind=engine, autoflush=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
