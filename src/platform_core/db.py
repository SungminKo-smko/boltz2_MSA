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

    if settings.database_url.startswith("postgresql"):
        # Supabase PgBouncer (transaction mode) 호환:
        # - NullPool: SQLAlchemy 자체 풀링 비활성화, PgBouncer에 위임
        # - prepare_threshold=0: prepared statements 비활성화
        kwargs["poolclass"] = NullPool
        kwargs["connect_args"] = {"prepare_threshold": 0}

    return create_engine(settings.database_url, **kwargs)


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


def init_db(*, create_tables: bool = True) -> None:
    from platform_core.models import Base  # noqa: F401
    import boltz2_service.models  # noqa: F401

    if create_tables:
        engine = get_engine()
        Base.metadata.create_all(bind=engine)
