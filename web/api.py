"""
web/api.py - FastAPI backend for ContosoForge.

Exposes the pipeline via REST + SSE,
consumed by the React SPA served from web/frontend/.

Launch:  python -m uvicorn web.api:app --port 8502
   or:   .\\scripts\\run_web.ps1
"""

from __future__ import annotations

import asyncio
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from src import __version__
from web.routes.config_routes import router as config_router
from web.routes.models_routes import router as models_router
from web.routes.generation_routes import (
    router as generation_router,
    shutdown_generation,
)
from web.routes.presets_routes import router as presets_router
from web.routes.data_routes import router as data_router
from web.routes.import_routes import router as import_router

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _lifespan(app: FastAPI):
    yield
    await asyncio.to_thread(shutdown_generation)


app = FastAPI(
    title="ContosoForge",
    version=__version__,
    lifespan=_lifespan,
)

_DEFAULT_CORS_ORIGINS = [
    "http://localhost:8502",
    "http://127.0.0.1:8502",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]
_cors_env = os.environ.get("CORS_ORIGINS", "")
_cors_origins = [o.strip() for o in _cors_env.split(",") if o.strip()] if _cors_env else _DEFAULT_CORS_ORIGINS

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type", "Authorization"],
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "font-src 'self'; "
            "connect-src 'self'; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'none'"
        )
        return response


app.add_middleware(SecurityHeadersMiddleware)

# ---------------------------------------------------------------------------
# Include routers — backward-compatible paths (no prefix)
# ---------------------------------------------------------------------------

app.include_router(config_router)
app.include_router(models_router)
app.include_router(generation_router)
app.include_router(presets_router)
app.include_router(data_router)
app.include_router(import_router)

# ---------------------------------------------------------------------------
# Include routers — versioned paths (/v1/api/...)
# ---------------------------------------------------------------------------

app.include_router(config_router, prefix="/v1")
app.include_router(models_router, prefix="/v1")
app.include_router(generation_router, prefix="/v1")
app.include_router(presets_router, prefix="/v1")
app.include_router(data_router, prefix="/v1")
app.include_router(import_router, prefix="/v1")

# ---------------------------------------------------------------------------
# Favicon (suppress browser 404)
# ---------------------------------------------------------------------------

_FAVICON_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    b'<rect width="32" height="32" rx="6" fill="#4f5bd5"/>'
    b'<text x="16" y="23" font-size="20" text-anchor="middle" fill="#fff" '
    b'font-family="system-ui">D</text></svg>'
)


@app.get("/favicon.ico")
def favicon():
    return Response(content=_FAVICON_SVG, media_type="image/svg+xml")


# ---------------------------------------------------------------------------
# Serve frontend
# ---------------------------------------------------------------------------

_FRONTEND_SOURCE_DIR = Path(__file__).resolve().parent / "frontend"
_FRONTEND_DIR = _FRONTEND_SOURCE_DIR / "dist"
_HASHED_ASSET_RE = re.compile(r"(?:^|/)[^/]+-[a-z0-9]{8,}\.(?:css|js)$", re.I)


class ProductionStaticFiles(StaticFiles):
    """Serve fingerprinted assets with long-lived immutable caching."""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            if _HASHED_ASSET_RE.search(path):
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            else:
                response.headers["Cache-Control"] = "no-cache"
        return response


@app.get("/", response_class=HTMLResponse)
def serve_frontend():
    index = _FRONTEND_DIR / "index.html"
    if not index.exists():
        raise HTTPException(
            404,
            "Frontend build not found. Restore committed web/frontend/dist assets "
            "or run 'npm ci && npm run build' in web/frontend.",
        )
    return HTMLResponse(
        content=index.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-cache"},
    )


if _FRONTEND_DIR.is_dir():
    app.mount(
        "/static",
        ProductionStaticFiles(directory=str(_FRONTEND_DIR)),
        name="static",
    )
