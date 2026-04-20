from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from platform_core.auth.api_key_auth import ApiKeyAuthService
from platform_core.auth.supabase_auth import SupabaseAuthError, verify_supabase_jwt
from platform_core.db import get_db_session
from platform_core.models.api_key import ApiKey
from platform_core.models.profile import Profile


def get_db() -> Session:
    yield from get_db_session()


def get_current_api_key(
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
    db: Session = Depends(get_db),
) -> ApiKey:
    return ApiKeyAuthService(db).authenticate(x_api_key)


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
) -> Profile:
    """Extract and verify Supabase JWT from Authorization header, return Profile."""
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Bearer token",
        )

    token = auth_header[len("Bearer "):]
    try:
        payload = verify_supabase_jwt(token)
    except SupabaseAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from exc

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="JWT missing 'sub' claim",
        )

    profile = db.query(Profile).filter(Profile.id == user_id).first()
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Profile not found — login via /auth/login first",
        )

    return profile
