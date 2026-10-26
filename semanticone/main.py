"""ASGI application factory."""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from . import __version__, api, billing, mcp
from .config import get_settings
from .db import get_engine, init_engine
from .engine.demo import ensure_demo_database
from .migrate import upgrade_database
from .web import admin, app_routes, public
from .web.deps import LoginRequired, render

log = logging.getLogger("semanticone")

STATIC_DIR = Path(__file__).parent / "web" / "static"


def create_app(database_url: str | None = None, run_migrations: bool = True) -> FastAPI:
    settings = get_settings()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout
    )
    init_engine(database_url)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if run_migrations:
            upgrade_database(get_engine())
        ensure_demo_database(settings.data_dir)
        yield

    app = FastAPI(
        title="SemanticOne API",
        version=__version__,
        description="Hosted semantic layer for Apache Ossie models. Authenticate with `Authorization: Bearer <api key>`.",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="so_session",
        max_age=60 * 60 * 24 * 14,
        same_site="lax",
        https_only=settings.secure_cookies,
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        if not request.url.path.startswith("/api/docs"):
            response.headers.setdefault("X-Frame-Options", "DENY")
        if settings.secure_cookies:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(public.router)
    app.include_router(app_routes.router)
    app.include_router(admin.router)
    app.include_router(billing.router)
    app.include_router(api.router)
    app.include_router(mcp.router)

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok", "version": __version__}

    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, _exc: LoginRequired):
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(target)}", status_code=303)

    @app.exception_handler(api.ApiError)
    async def api_error(_request: Request, exc: api.ApiError):
        return api.error_response(exc)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        wants_json = request.url.path.startswith(("/api/", "/mcp", "/webhooks/")) or "application/json" in request.headers.get(
            "accept", ""
        )
        if wants_json:
            return JSONResponse({"error": {"message": exc.detail, "status": exc.status_code}}, status_code=exc.status_code)
        return render(
            request,
            "error.html",
            None,
            status_code=exc.status_code,
            code=exc.status_code,
            detail=exc.detail if exc.status_code != 404 else "We couldn't find that page.",
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        errors = [{"loc": list(e.get("loc", [])), "msg": e.get("msg")} for e in exc.errors()]
        return JSONResponse({"error": {"message": "Invalid request", "status": 422, "details": errors}}, status_code=422)

    return app


def app_factory() -> FastAPI:  # pragma: no cover - used by uvicorn --factory
    return create_app()
