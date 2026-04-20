from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from platform_core.config import get_settings, register_settings
from platform_core.db import init_db


def create_app() -> FastAPI:
    from boltz2_service.config import get_settings as get_boltz2_settings
    from boltz2_service.mcp.server import mcp

    # Register boltz2-specific settings so platform_core can access them
    register_settings(get_boltz2_settings())

    mcp_starlette = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            init_db(model_modules=["boltz2_service.models"])
        except Exception as e:
            import structlog
            structlog.get_logger().warning("db_init_deferred", error=str(e))
        async with mcp.session_manager.run():
            yield

    app = FastAPI(
        title="Bio AI Platform — Boltz-2 Service",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    from boltz2_service.api.routes.health import router as health_router
    from boltz2_service.api.routes.auth import router as auth_router
    from boltz2_service.api.routes.uploads import router as uploads_router
    from boltz2_service.api.routes.specs import router as specs_router
    from boltz2_service.api.routes.jobs import router as jobs_router

    app.include_router(health_router, tags=["health"])
    app.include_router(auth_router)
    app.include_router(uploads_router)
    app.include_router(specs_router)
    app.include_router(jobs_router)

    # Root-level .well-known — Claude Code discovers OAuth here (RFC 9728)
    settings = get_settings()
    issuer = settings.mcp_issuer_url

    @app.get("/.well-known/oauth-protected-resource")
    @app.get("/.well-known/oauth-protected-resource/{path:path}")
    async def oauth_protected_resource(path: str = ""):
        return JSONResponse({
            "resource": f"{issuer}/mcp",
            "authorization_servers": [issuer],
            "scopes_supported": ["boltz2"],
        })

    @app.get("/.well-known/oauth-authorization-server")
    @app.get("/.well-known/oauth-authorization-server/{path:path}")
    async def oauth_auth_server(path: str = ""):
        return JSONResponse({
            "issuer": issuer,
            "authorization_endpoint": f"{issuer}/authorize",
            "token_endpoint": f"{issuer}/token",
            "registration_endpoint": f"{issuer}/register",
            "scopes_supported": ["boltz2"],
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
            "revocation_endpoint": f"{issuer}/revoke",
            "revocation_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
            "code_challenge_methods_supported": ["S256"],
        })

    @app.get("/.well-known/openid-configuration")
    @app.get("/.well-known/openid-configuration/{path:path}")
    async def openid_config(path: str = ""):
        return JSONResponse({
            "issuer": issuer,
            "authorization_endpoint": f"{issuer}/authorize",
            "token_endpoint": f"{issuer}/token",
            "registration_endpoint": f"{issuer}/register",
        })

    # MCP Streamable HTTP
    app.mount("/mcp", mcp_starlette)

    return app
