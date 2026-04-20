from __future__ import annotations

from functools import lru_cache

import httpx
from jose import JWTError, jwt

from platform_core.config import get_settings


class SupabaseAuthError(Exception):
    pass


@lru_cache(maxsize=1)
def _fetch_jwks() -> dict:
    """Fetch JWKS from Supabase for ES256 verification."""
    settings = get_settings()
    url = f"{settings.supabase_url}/auth/v1/.well-known/jwks.json"
    resp = httpx.get(url, timeout=10)
    resp.raise_for_status()
    return resp.json()


def verify_supabase_jwt(token: str) -> dict:
    """Verify a Supabase JWT and return the payload.

    Supports both HS256 (legacy) and ES256 (current) signing.
    Returns dict with at least: sub (user_id), email, aud.
    Raises SupabaseAuthError on invalid/expired tokens.
    """
    settings = get_settings()

    # Peek at the header to determine algorithm
    try:
        header = jwt.get_unverified_header(token)
    except JWTError as exc:
        raise SupabaseAuthError(f"Invalid JWT header: {exc}") from exc

    alg = header.get("alg", "HS256")

    try:
        if alg == "ES256":
            jwks = _fetch_jwks()
            kid = header.get("kid")
            key = None
            for k in jwks.get("keys", []):
                if k.get("kid") == kid:
                    key = k
                    break
            if key is None:
                # Invalidate cache and retry once
                _fetch_jwks.cache_clear()
                jwks = _fetch_jwks()
                for k in jwks.get("keys", []):
                    if k.get("kid") == kid:
                        key = k
                        break
            if key is None:
                raise SupabaseAuthError(f"JWKS key not found for kid={kid}")

            payload = jwt.decode(
                token,
                key,
                algorithms=["ES256"],
                audience="authenticated",
            )
        else:
            secret = settings.supabase_jwt_secret.get_secret_value()
            if not secret:
                raise SupabaseAuthError("Supabase JWT secret not configured")
            payload = jwt.decode(
                token,
                secret,
                algorithms=["HS256"],
                audience="authenticated",
            )
    except JWTError as exc:
        raise SupabaseAuthError(f"Invalid JWT: {exc}") from exc

    return payload


def extract_email_domain(email: str) -> str | None:
    if "@" in email:
        return email.split("@", 1)[1].lower()
    return None
