from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from fastapi import HTTPException
from sqlalchemy.orm import Session

from platform_core.auth.api_key_auth import ApiKeyAuthService
from platform_core.db import SessionLocal
from platform_core.models.api_key import ApiKey


@contextmanager
def mcp_auth(api_key: str) -> Generator[tuple[Session, ApiKey], None, None]:
    """MCP tool auth context: yields (db_session, authenticated_api_key).

    Converts HTTPException to ValueError since HTTP semantics don't apply in MCP.
    """
    db = SessionLocal()
    try:
        try:
            key = ApiKeyAuthService(db).authenticate(api_key)
        except HTTPException as e:
            raise ValueError(e.detail) from None
        yield db, key
    finally:
        db.close()
