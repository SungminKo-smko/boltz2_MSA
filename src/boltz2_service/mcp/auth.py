from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

import structlog
from fastapi import HTTPException
from sqlalchemy.orm import Session

from platform_core.auth.api_key_auth import ApiKeyAuthService
from platform_core.db import SessionLocal
from platform_core.models.api_key import ApiKey

logger = structlog.get_logger(__name__)


@contextmanager
def mcp_auth(api_key: str = "") -> Generator[tuple[Session, ApiKey], None, None]:
    """MCP tool auth context: yields (db_session, authenticated_api_key).

    Checks for Bearer token from OAuth context first, falls back to explicit api_key.
    Converts HTTPException to ValueError since HTTP semantics don't apply in MCP.
    """
    effective_key = api_key
    if not effective_key:
        try:
            from mcp.server.auth.middleware.auth_context import get_access_token

            access_token = get_access_token()
            if access_token:
                effective_key = access_token.token
                logger.debug("mcp_auth_bearer_token_found", token_prefix=effective_key[:8] + "...")
            else:
                logger.warning("mcp_auth_no_bearer_token")
        except Exception as exc:
            logger.warning("mcp_auth_bearer_lookup_failed", error=str(exc))

    if not effective_key:
        raise ValueError(
            "Authentication required. Connect via MCP OAuth (browser login) "
            "or pass api_key explicitly."
        )

    db = SessionLocal()
    try:
        try:
            key = ApiKeyAuthService(db).authenticate(effective_key)
        except HTTPException as e:
            raise ValueError(e.detail) from None
        yield db, key
    finally:
        db.close()
