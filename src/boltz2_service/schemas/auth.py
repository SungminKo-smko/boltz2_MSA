from __future__ import annotations

from pydantic import BaseModel


class AuthCallbackResponse(BaseModel):
    """Returned after successful OAuth callback."""

    user_id: str
    email: str
    display_name: str | None = None
    is_approved: bool
    api_key: str | None = None  # plaintext key, only returned once
    message: str


class ProfileResponse(BaseModel):
    """Public profile info (no secrets)."""

    user_id: str
    email: str
    display_name: str | None = None
    is_approved: bool
    has_api_key: bool


# --- Device Authorization Flow ---


class DeviceCodeRequest(BaseModel):
    """Body for POST /auth/device-code (all fields optional)."""

    client_name: str | None = None


class DeviceCodeResponse(BaseModel):
    device_code: str
    user_code: str
    verification_url: str
    expires_in: int


class DeviceTokenRequest(BaseModel):
    device_code: str


class DeviceTokenResponse(BaseModel):
    status: str
    api_key: str | None = None
    error: str | None = None
