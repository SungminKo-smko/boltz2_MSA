from __future__ import annotations

import secrets
from datetime import timedelta
from urllib.parse import urlencode
from uuid import uuid4

import httpx
import structlog
from cachetools import TTLCache
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from boltz2_service.api.deps import get_current_user, get_db
from boltz2_service.schemas.auth import (
    AuthCallbackResponse,
    DeviceCodeRequest,
    DeviceCodeResponse,
    DeviceTokenRequest,
    DeviceTokenResponse,
    ProfileResponse,
)
from boltz2_service.services.auth_hooks import on_user_authenticated
from platform_core.auth.domain_rules import get_domain_rule
from platform_core.auth.supabase_auth import SupabaseAuthError, verify_supabase_jwt
from platform_core.config import get_settings
from platform_core.models.api_key import ApiKey
from platform_core.models.device_code import DeviceCode, DeviceCodeStatus
from platform_core.models.profile import Profile
from platform_core.security import create_api_key as create_api_key_pair
from platform_core.time_utils import utc_now

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


def _find_active_api_key(db: Session, profile_id: str, service: str = "boltz2") -> ApiKey | None:
    return db.scalar(
        select(ApiKey).where(
            ApiKey.profile_id == profile_id,
            ApiKey.service == service,
            ApiKey.is_active.is_(True),
        )
    )


@router.get("/login")
async def login(request: Request):
    """Start Google OAuth via Supabase — redirect to Supabase Auth URL."""
    settings = get_settings()
    callback_url = str(request.url_for("auth_callback"))

    params = urlencode({
        "provider": "google",
        "redirect_to": callback_url,
    })
    auth_url = f"{settings.supabase_url}/auth/v1/authorize?{params}"

    return {"auth_url": auth_url}


@router.get("/callback", name="auth_callback")
async def callback(code: str, db: Session = Depends(get_db)):
    """Supabase OAuth callback.

    Exchanges the auth code for a session, verifies the JWT,
    upserts the profile, and auto-issues an API key for approved domains.
    """
    settings = get_settings()

    token_url = f"{settings.supabase_url}/auth/v1/token?grant_type=authorization_code"
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            token_url,
            json={"auth_code": code},
            headers={
                "apikey": settings.supabase_anon_key,
                "Content-Type": "application/json",
            },
        )

    if resp.status_code != 200:
        logger.error("token_exchange_failed", status=resp.status_code, body=resp.text)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Failed to exchange auth code: {resp.text}",
        )

    token_data = resp.json()
    access_token = token_data.get("access_token")
    if not access_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No access token in response",
        )

    try:
        payload = verify_supabase_jwt(access_token)
    except SupabaseAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from exc

    user_id = payload.get("sub")
    email = payload.get("email", "")
    user_meta = payload.get("user_metadata", {})
    display_name = user_meta.get("full_name") or user_meta.get("name")

    if not user_id or not email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="JWT missing required claims (sub, email)",
        )

    rule = get_domain_rule(email)
    auto_approved = rule is not None and rule.auto_approve

    profile = db.query(Profile).filter(Profile.id == user_id).first()
    if profile is None:
        profile = Profile(
            id=user_id,
            email=email,
            display_name=display_name,
            is_approved=auto_approved,
            auto_approved=auto_approved,
        )
        db.add(profile)
        logger.info("profile_created", user_id=user_id, email=email)
    elif display_name and profile.display_name != display_name:
        profile.display_name = display_name
        profile.updated_at = utc_now()

    raw_key = on_user_authenticated(profile, db)
    db.commit()

    message = "Login successful."
    if raw_key:
        message += " API key has been automatically issued."
    elif not profile.is_approved:
        message += " Your account is pending admin approval."

    return AuthCallbackResponse(
        user_id=user_id,
        email=email,
        display_name=profile.display_name,
        is_approved=profile.is_approved,
        api_key=raw_key,
        message=message,
    )


@router.get("/me")
async def get_me(
    profile: Profile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the current user's profile (requires Bearer JWT)."""
    has_key = _find_active_api_key(db, profile.id) is not None

    return ProfileResponse(
        user_id=profile.id,
        email=profile.email,
        display_name=profile.display_name,
        is_approved=profile.is_approved,
        has_api_key=has_key,
    )


# ---------------------------------------------------------------------------
# Device Authorization Flow
# ---------------------------------------------------------------------------

DEVICE_CODE_TTL = timedelta(minutes=15)

# Plaintext keys are held in memory between verify and token endpoints.
# TTL matches DEVICE_CODE_TTL; maxsize prevents unbounded growth.
_device_plaintext_keys: TTLCache[str, str] = TTLCache(maxsize=1024, ttl=900)

_USER_CODE_MAX_RETRIES = 5


def _generate_user_code() -> str:
    """Generate an 8-char uppercase code with a hyphen: ABCD-EFGH."""
    raw = secrets.token_hex(4).upper()
    return f"{raw[:4]}-{raw[4:]}"


@router.post("/device-code", response_model=DeviceCodeResponse)
async def request_device_code(
    request: Request,
    body: DeviceCodeRequest | None = None,
    db: Session = Depends(get_db),
):
    """Issue a new device code pair (public endpoint, no auth required)."""
    now = utc_now()

    # Retry on user_code collision (hex-only gives ~65K codes)
    for _ in range(_USER_CODE_MAX_RETRIES):
        user_code = _generate_user_code()
        existing = db.scalar(
            select(DeviceCode.id).where(DeviceCode.user_code == user_code)
        )
        if existing is None:
            break
    else:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not generate unique user code, try again",
        )

    dc = DeviceCode(
        device_code=str(uuid4()),
        user_code=user_code,
        expires_at=now + DEVICE_CODE_TTL,
        status=DeviceCodeStatus.PENDING,
    )
    db.add(dc)
    db.commit()

    verification_url = str(request.url_for("verify_device_code"))
    verification_url += f"?user_code={dc.user_code}"

    return DeviceCodeResponse(
        device_code=dc.device_code,
        user_code=dc.user_code,
        verification_url=verification_url,
        expires_in=int(DEVICE_CODE_TTL.total_seconds()),
    )


@router.get("/device-verify", name="verify_device_code")
async def verify_device_code(
    user_code: str,
    profile: Profile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Browser-side: authenticated user approves a device code."""
    dc = db.scalar(
        select(DeviceCode).where(DeviceCode.user_code == user_code)
    )
    if dc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Unknown user code",
        )

    now = utc_now()
    if dc.expires_at <= now or dc.status == DeviceCodeStatus.EXPIRED:
        dc.status = DeviceCodeStatus.EXPIRED
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Device code expired",
        )

    if dc.status != DeviceCodeStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Device code already {dc.status}",
        )

    dc.profile_id = profile.id

    # Try auto-issue via domain hook first; if key already exists, create a device-specific one
    raw_key = on_user_authenticated(profile, db)
    if not raw_key:
        if not profile.is_approved:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Account not approved for API key issuance",
            )
        raw_key, key_hash = create_api_key_pair(prefix="b2")
        device_key = ApiKey(
            profile_id=profile.id,
            service="boltz2",
            name="device",
            key_hash=key_hash,
        )
        db.add(device_key)
        db.flush()
        dc.api_key_id = device_key.id
    else:
        key = _find_active_api_key(db, profile.id)
        if key:
            dc.api_key_id = key.id

    dc.status = DeviceCodeStatus.AUTHORIZED
    _device_plaintext_keys[dc.device_code] = raw_key
    db.commit()

    return {"status": "authorized", "message": "Device authorized. You may close this page."}


@router.post("/device-token")
async def poll_device_token(
    body: DeviceTokenRequest,
    db: Session = Depends(get_db),
):
    """MCP client polls this endpoint to receive the API key."""
    dc = db.scalar(
        select(DeviceCode).where(DeviceCode.device_code == body.device_code)
    )
    if dc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Unknown device code",
        )

    now = utc_now()

    if dc.expires_at <= now and dc.status == DeviceCodeStatus.PENDING:
        dc.status = DeviceCodeStatus.EXPIRED
        db.commit()

    if dc.status == DeviceCodeStatus.EXPIRED:
        return JSONResponse(
            status_code=status.HTTP_410_GONE,
            content={"error": "expired"},
        )

    if dc.status == DeviceCodeStatus.USED:
        return JSONResponse(
            status_code=status.HTTP_410_GONE,
            content={"error": "already_used"},
        )

    if dc.status == DeviceCodeStatus.PENDING:
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={"status": "pending"},
        )

    plaintext = _device_plaintext_keys.pop(dc.device_code, None)
    dc.status = DeviceCodeStatus.USED
    db.commit()

    return DeviceTokenResponse(
        status="authorized",
        api_key=plaintext,
    )
