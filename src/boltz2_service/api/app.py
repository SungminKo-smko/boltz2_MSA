from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from platform_core.db import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


def create_app() -> FastAPI:
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

    # MCP Streamable HTTP endpoint (/mcp)
    from boltz2_service.mcp.server import get_mcp_app

    app.mount("/mcp", get_mcp_app())

    return app
