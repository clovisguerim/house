from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SearchHistory(Base):
    __tablename__ = "search_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    query: Mapped[str] = mapped_column(Text)
    filters_json: Mapped[str] = mapped_column(Text)
    total_results: Mapped[int] = mapped_column(Integer, default=0)
    # Resposta completa da busca (SearchResponse em JSON) para reabrir depois
    results_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Último relatório da IA sobre esta busca (JSON), para reabrir sem gerar de novo
    relatorio_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Regra(Base):
    """Preferência aprendida (de um feedback) ou escrita à mão, usada pela IA em buscas e relatórios.

    Compartilhada = vale para todos os usuários; senão só para o autor."""
    __tablename__ = "regras"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    texto: Mapped[str] = mapped_column(Text)
    compartilhada: Mapped[bool] = mapped_column(Boolean, default=True)
    ativa: Mapped[bool] = mapped_column(Boolean, default=True)
    origem: Mapped[str] = mapped_column(String(16), default="manual")  # "manual" ou "feedback"
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Feedback(Base):
    """Avaliação do usuário sobre o relatório da IA (inteiro ou um imóvel dele)."""
    __tablename__ = "feedbacks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    historico_id: Mapped[int] = mapped_column(ForeignKey("search_history.id", ondelete="CASCADE"), index=True)
    imovel_id: Mapped[str | None] = mapped_column(String(128), nullable=True)  # "fonte:id"; vazio = relatório todo
    nota: Mapped[int] = mapped_column(Integer)  # 1 a 5
    justificativa: Mapped[str] = mapped_column(Text)
    regra_id: Mapped[int | None] = mapped_column(ForeignKey("regras.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class HttpCache(Base):
    __tablename__ = "http_cache"

    url: Mapped[str] = mapped_column(String(1024), primary_key=True)
    body: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[float] = mapped_column(Float)
