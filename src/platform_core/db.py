from __future__ import annotations

from collections.abc import Generator
from functools import lru_cache

from sqlalchemy import create_engine, NullPool
from sqlalchemy.orm import Session, sessionmaker


@lru_cache(maxsize=1)
def get_engine():
    from platform_core.config import get_settings

    settings = get_settings()
    kwargs: dict = {"future": True}

    url = settings.database_url

    if url.startswith("postgresql"):
        # Normalize scheme so SQLAlchemy uses psycopg (v3), not the legacy psycopg2.
        # Supabase/Render often provide plain "postgresql://..." URLs.
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        elif url.startswith("postgresql+psycopg2://"):
            url = url.replace("postgresql+psycopg2://", "postgresql+psycopg://", 1)

        # Supabase Pooler (transaction mode) 호환:
        # - NullPool: SQLAlchemy 자체 풀링 비활성화
        # - prepare_threshold=0: prepared statements 비활성화
        # - options: statement-level prepare threshold override
        kwargs["poolclass"] = NullPool
        kwargs["connect_args"] = {"prepare_threshold": 0}

    return create_engine(url, **kwargs)


@lru_cache(maxsize=1)
def get_session_factory():
    return sessionmaker(
        bind=get_engine(),
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )


def SessionLocal() -> Session:
    return get_session_factory()()


def get_db_session() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db(*, create_tables: bool = True, model_modules: list[str] | None = None) -> None:
    """Initialize the database, optionally creating tables.

    Args:
        create_tables: Whether to run CREATE TABLE statements.
        model_modules: List of dotted module paths to import so that their
            SQLAlchemy models are registered on Base.metadata before
            create_all is called.  Example: ["boltz2_service.models"]
    """
    import importlib

    from platform_core.models import Base  # noqa: F401

    for mod in model_modules or []:
        importlib.import_module(mod)

    if create_tables:
        engine = get_engine()
        Base.metadata.create_all(bind=engine)
