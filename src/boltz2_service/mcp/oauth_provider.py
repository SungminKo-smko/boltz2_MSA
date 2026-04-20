"""OAuth 2.1 Authorization Server Provider for Boltz-2 MCP.

Bridges Claude Code's OAuth flow to Supabase Google OAuth.
Flow: Claude Code → MCP OAuth → Supabase Google → API Key issued → Bearer token.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
import structlog
from cachetools import TTLCache
from mcp.server.auth.provider import (
    AuthorizationCode,
    AuthorizationParams,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from boltz2_service.services.auth_hooks import on_user_authenticated
from platform_core.auth.domain_rules import get_domain_rule
from platform_core.auth.supabase_auth import SupabaseAuthError, verify_supabase_jwt
from platform_core.config import get_settings
from platform_core.db import SessionLocal
from platform_core.models.api_key import ApiKey
from platform_core.models.profile import Profile
from platform_core.security import create_api_key as create_api_key_pair

logger = structlog.get_logger(__name__)


@dataclass
class _AccessTokenInfo:
    token: str
    client_id: str
    scopes: list[str]
    expires_at: float | None = None


@dataclass
class _RefreshTokenInfo:
    token: str
    client_id: str
    scopes: list[str]
    api_key_plaintext: str


@dataclass
class _AuthSession:
    """Stores OAuth state between authorize() and the Supabase callback."""
    client_id: str
    params: AuthorizationParams


class Boltz2OAuthProvider:
    """OAuthAuthorizationServerProvider bridging MCP OAuth to Supabase Google OAuth."""

    def __init__(self) -> None:
        # Dynamic client registration (transient, 1 hour TTL)
        self._clients: TTLCache[str, OAuthClientInformationFull] = TTLCache(maxsize=256, ttl=3600)
        # Authorization sessions waiting for Supabase callback
        self._auth_sessions: TTLCache[str, _AuthSession] = TTLCache(maxsize=256, ttl=600)
        # Authorization codes waiting to be exchanged
        self._auth_codes: TTLCache[str, AuthorizationCode] = TTLCache(maxsize=256, ttl=300)
        # Map auth_code → plaintext API key
        self._code_to_api_key: TTLCache[str, str] = TTLCache(maxsize=256, ttl=300)
        # Refresh tokens
        self._refresh_tokens: TTLCache[str, _RefreshTokenInfo] = TTLCache(maxsize=256, ttl=86400)
        # Access tokens (API keys are validated against DB, but we cache metadata)
        self._access_tokens: TTLCache[str, _AccessTokenInfo] = TTLCache(maxsize=1024, ttl=3600)

    # -- Client Registration --------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if client_info.client_id is None:
            client_info.client_id = f"mcp-{secrets.token_urlsafe(16)}"
        # Don't force a client_secret — Claude Code is a public client using PKCE
        client_info.client_id_issued_at = int(time.time())
        self._clients[client_info.client_id] = client_info
        logger.info("oauth_client_registered", client_id=client_info.client_id)

    # -- Authorization ---------------------------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        settings = get_settings()
        session_id = secrets.token_urlsafe(32)

        self._auth_sessions[session_id] = _AuthSession(
            client_id=client.client_id,
            params=params,
        )

        # Build Supabase Google OAuth URL, redirect back to our callback
        # session_id in path (not query) to match Supabase redirect URL registration
        callback_url = f"{settings.mcp_issuer_url}/oauth/callback/{session_id}"
        supabase_params = urlencode({
            "provider": "google",
            "redirect_to": callback_url,
        })
        auth_url = f"{settings.supabase_url}/auth/v1/authorize?{supabase_params}"

        logger.info("oauth_authorize_redirect", session_id=session_id, client_id=client.client_id)
        return auth_url

    # -- Authorization Code Exchange -------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        return self._auth_codes.get(authorization_code)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        api_key_plaintext = self._code_to_api_key.pop(authorization_code.code, None)
        if not api_key_plaintext:
            raise ValueError("Authorization code has no associated API key")

        # Remove used auth code
        self._auth_codes.pop(authorization_code.code, None)

        # Generate refresh token
        refresh_token = secrets.token_urlsafe(32)
        self._refresh_tokens[refresh_token] = _RefreshTokenInfo(
            token=refresh_token,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            api_key_plaintext=api_key_plaintext,
        )

        # Cache access token metadata
        self._access_tokens[api_key_plaintext] = _AccessTokenInfo(
            token=api_key_plaintext,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=time.time() + 86400,
        )

        logger.info("oauth_code_exchanged", client_id=client.client_id)
        return OAuthToken(
            access_token=api_key_plaintext,
            token_type="Bearer",
            expires_in=86400,
            refresh_token=refresh_token,
            scope=" ".join(authorization_code.scopes) if authorization_code.scopes else None,
        )

    # -- Token Validation ------------------------------------------------------

    async def load_access_token(self, token: str) -> _AccessTokenInfo | None:
        cached = self._access_tokens.get(token)
        if cached:
            return cached

        # Validate against DB (API key lookup)
        from platform_core.security import hash_api_key

        db = SessionLocal()
        try:
            from sqlalchemy import select
            api_key = db.scalar(
                select(ApiKey).where(ApiKey.key_hash == hash_api_key(token))
            )
            if api_key is None or not api_key.is_active:
                return None

            info = _AccessTokenInfo(
                token=token,
                client_id="api-key-direct",
                scopes=[],
            )
            self._access_tokens[token] = info
            return info
        finally:
            db.close()

    # -- Refresh Token ---------------------------------------------------------

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> _RefreshTokenInfo | None:
        return self._refresh_tokens.get(refresh_token)

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull,
        refresh_token: _RefreshTokenInfo,
        scopes: list[str],
    ) -> OAuthToken:
        # Rotate refresh token
        old_token = refresh_token.token
        new_refresh = secrets.token_urlsafe(32)
        self._refresh_tokens.pop(old_token, None)
        self._refresh_tokens[new_refresh] = _RefreshTokenInfo(
            token=new_refresh,
            client_id=client.client_id,
            scopes=scopes or refresh_token.scopes,
            api_key_plaintext=refresh_token.api_key_plaintext,
        )

        # Re-cache access token
        self._access_tokens[refresh_token.api_key_plaintext] = _AccessTokenInfo(
            token=refresh_token.api_key_plaintext,
            client_id=client.client_id,
            scopes=scopes or refresh_token.scopes,
            expires_at=time.time() + 86400,
        )

        return OAuthToken(
            access_token=refresh_token.api_key_plaintext,
            token_type="Bearer",
            expires_in=86400,
            refresh_token=new_refresh,
            scope=" ".join(scopes) if scopes else None,
        )

    # -- Revocation ------------------------------------------------------------

    async def revoke_token(self, token: _AccessTokenInfo | _RefreshTokenInfo) -> None:
        if isinstance(token, _AccessTokenInfo):
            self._access_tokens.pop(token.token, None)
        elif isinstance(token, _RefreshTokenInfo):
            self._refresh_tokens.pop(token.token, None)
            self._access_tokens.pop(token.api_key_plaintext, None)

    # -- Supabase OAuth Callback -----------------------------------------------

    async def handle_oauth_callback(self, request: Request) -> Response:
        """Handle Supabase OAuth callback, issue MCP authorization code.

        Supabase sends tokens as hash fragments (#access_token=...) which aren't
        sent to the server. Two-phase approach:
        - Phase 1 (GET without access_token param): serve HTML that reads hash and POSTs it
        - Phase 2 (POST with access_token param): process the token server-side
        """
        path = request.url.path.rstrip("/")
        session_id = path.rsplit("/", 1)[-1] if "/" in path else None

        if not session_id:
            return JSONResponse({"error": "Missing session_id"}, status_code=400)

        # Phase 2: POST with access_token from the HTML form
        if request.method == "POST":
            content_type = request.headers.get("content-type", "")
            access_token = None
            if "form" in content_type:
                form = await request.form()
                access_token = form.get("access_token")
            elif "json" in content_type:
                body = await request.json()
                access_token = body.get("access_token")
            if not access_token:
                return JSONResponse({"error": "Missing access_token in POST"}, status_code=400)
            return await self._process_token(session_id, str(access_token))

        # Phase 1: Supabase redirects here with #access_token=... in hash
        # Check if code is in query params (PKCE flow)
        code = request.query_params.get("code")
        if code:
            return await self._process_code(session_id, code)

        # Serve HTML that extracts hash fragment and submits via form POST
        # Form submit follows server redirects natively (faster than fetch + JS redirect)
        settings = get_settings()
        callback_post_url = f"{settings.mcp_issuer_url}/oauth/callback/{session_id}"
        html = f"""<!DOCTYPE html>
<html><head><title>Boltz-2 Authorization</title></head>
<body>
<p>Completing authorization...</p>
<form id="f" method="POST" action="{callback_post_url}">
<input type="hidden" name="access_token" id="at">
</form>
<script>
var h=window.location.hash.substring(1);
var t=new URLSearchParams(h).get('access_token');
if(t){{document.getElementById('at').value=t;document.getElementById('f').submit();}}
else{{document.body.innerHTML='<p>Error: No access token received.</p>';}}
</script>
</body></html>"""
        from starlette.responses import HTMLResponse
        return HTMLResponse(html)

    async def _process_code(self, session_id: str, code: str) -> Response:
        """Process Supabase authorization code (PKCE flow)."""
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
            return JSONResponse({"error": f"Token exchange failed: {resp.text}"}, status_code=502)
        token_data = resp.json()
        access_token = token_data.get("access_token")
        if not access_token:
            return JSONResponse({"error": "No access token"}, status_code=502)
        return await self._process_token(session_id, access_token)

    async def _process_token(self, session_id: str, access_token: str) -> Response:
        """Process a verified Supabase JWT, issue MCP authorization code.
        Returns a 302 redirect to Claude Code's callback URL."""
        session = self._auth_sessions.pop(session_id, None)
        if session is None:
            return JSONResponse({"error": "Session expired or not found"}, status_code=410)

        try:
            payload = verify_supabase_jwt(access_token)
        except SupabaseAuthError as exc:
            return JSONResponse({"error": str(exc)}, status_code=401)

        user_id = payload.get("sub")
        email = payload.get("email", "")
        user_meta = payload.get("user_metadata", {})
        display_name = user_meta.get("full_name") or user_meta.get("name")

        if not user_id or not email:
            return JSONResponse({"error": "JWT missing sub or email"}, status_code=401)

        # Upsert profile and get/create API key
        db = SessionLocal()
        try:
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

            raw_key = on_user_authenticated(profile, db)

            if not raw_key:
                if not profile.is_approved:
                    db.commit()
                    return JSONResponse(
                        {"error": "Account not approved for API key issuance"},
                        status_code=403,
                    )
                # Check for existing active key before creating new one
                from sqlalchemy import select
                existing = db.scalar(
                    select(ApiKey).where(
                        ApiKey.profile_id == profile.id,
                        ApiKey.service == "boltz2",
                        ApiKey.is_active.is_(True),
                    )
                )
                if existing:
                    # Generate a fresh plaintext key for this session
                    raw_key, key_hash = create_api_key_pair(prefix="b2")
                    existing.key_hash = key_hash
                    existing.name = "mcp-oauth"
                else:
                    raw_key, key_hash = create_api_key_pair(prefix="b2")
                    new_key = ApiKey(
                        profile_id=profile.id,
                        service="boltz2",
                        name="mcp-oauth",
                        key_hash=key_hash,
                    )
                    db.add(new_key)

            db.commit()
        finally:
            db.close()

        # Generate MCP authorization code (160+ bits entropy)
        mcp_auth_code = secrets.token_urlsafe(24)  # 192 bits

        self._auth_codes[mcp_auth_code] = AuthorizationCode(
            code=mcp_auth_code,
            scopes=session.params.scopes or [],
            expires_at=time.time() + 300,
            client_id=session.client_id,
            code_challenge=session.params.code_challenge,
            redirect_uri=session.params.redirect_uri,
            redirect_uri_provided_explicitly=session.params.redirect_uri_provided_explicitly,
        )
        self._code_to_api_key[mcp_auth_code] = raw_key

        redirect_params = {"code": mcp_auth_code}
        if session.params.state:
            redirect_params["state"] = session.params.state
        redirect_url = f"{session.params.redirect_uri}?{urlencode(redirect_params)}"

        logger.info("oauth_callback_success", email=email, client_id=session.client_id)
        return RedirectResponse(url=redirect_url, status_code=302)
