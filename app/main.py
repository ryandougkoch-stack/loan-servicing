"""
app/main.py

FastAPI application factory.
"""
from pathlib import Path

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import settings
import app.models  # ensure all models are registered
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import RequestIDMiddleware, TenantContextMiddleware

logger = structlog.get_logger(__name__)


def create_app() -> FastAPI:
    configure_logging()

    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        docs_url="/docs" if not settings.is_production else None,
        redoc_url="/redoc" if not settings.is_production else None,
        openapi_url="/openapi.json" if not settings.is_production else None,
    )

    # -------------------------------------------------------------------------
    # Middleware (order matters — outermost first)
    # -------------------------------------------------------------------------
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(TenantContextMiddleware)
    app.add_middleware(RequestIDMiddleware)

    # -------------------------------------------------------------------------
    # Exception handlers
    # -------------------------------------------------------------------------
    register_exception_handlers(app)

    # -------------------------------------------------------------------------
    # Routers
    # -------------------------------------------------------------------------
    from app.api.v1.router import api_router
    app.include_router(api_router, prefix=settings.API_PREFIX)

    # Static assets + dashboard SPA. dashboard.html is a single-file vanilla-JS
    # app that calls the JSON API directly with bearer JWT — served from
    # disk; the FastAPI process itself is just the API backend.
    static_dir = Path(__file__).parent / "static"
    static_dir.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    dashboard_file = static_dir / "dashboard.html"

    @app.get("/", include_in_schema=False)
    async def serve_dashboard():
        return FileResponse(str(dashboard_file))

    @app.get("/dashboard.html", include_in_schema=False)
    async def serve_dashboard_legacy():
        # Honour the original filename people may have bookmarked.
        return FileResponse(str(dashboard_file))

    # -------------------------------------------------------------------------
    # Startup / shutdown
    # -------------------------------------------------------------------------
    @app.on_event("startup")
    async def startup():
        print(f"Starting {settings.APP_NAME} v{settings.APP_VERSION} [{settings.APP_ENV}]")

    @app.on_event("shutdown")
    async def shutdown():
        from app.db.session import engine
        await engine.dispose()
        logger.info("app_shutdown")

    # -------------------------------------------------------------------------
    # Health check (no auth required)
    # -------------------------------------------------------------------------
    @app.get("/health", tags=["health"], include_in_schema=False)
    async def health():
        return {"status": "ok", "version": settings.APP_VERSION}

    return app


app = create_app()
