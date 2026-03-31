from __future__ import annotations

from jose import JWTError, jwt

from platform_core.config import get_settings


class SupabaseAuthError(Exception):
    pass


def verify_supabase_jwt(token: str) -> dict:
    """Verify a Supabase JWT and return the payload.

    Returns dict with at least: sub (user_id), email, aud.
    Raises SupabaseAuthError on invalid/expired tokens.
    """
    settings = get_settings()
    secret = settings.supabase_jwt_secret.get_secret_value()
    if not secret:
        raise SupabaseAuthError("Supabase JWT secret not configured")
    try:
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
