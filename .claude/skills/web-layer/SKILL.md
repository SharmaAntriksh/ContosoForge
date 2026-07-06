---
name: web-layer
description: >-
  Conventions and safety rules for the ContosoForge web layer — the FastAPI backend
  (web/api.py, web/routes/, web/shared_state.py) and its React SPA (web/frontend/).
  Load this when editing anything under web/ — adding or changing an API endpoint,
  touching shared mutable state, or working on CORS/security/payload handling. It
  covers the thread-safety locking discipline, the dual (/api + /v1/api) route
  versioning, the CORS allowlist and security headers, and the request-size limits.
  Use it for any "add an endpoint", "the web UI", "FastAPI route", or "why is the
  API rejecting my request" work.
---

# Web Layer: FastAPI Backend + React SPA

The web UI is a FastAPI backend serving a React SPA. `api.py` is the app entry point;
routers are split by concern under `web/routes/`; mutable server state lives in
`web/shared_state.py`.

## Layout

```
web/
  api.py               # FastAPI app: mounts routers, CORS, security middleware, serves SPA
  shared_state.py      # shared mutable state (_cfg, _models_cfg, ...) + _cfg_lock
  routes/
    config_routes.py     # /api/config
    models_routes.py     # /api/models
    generation_routes.py # /api/generate, /api/validate
    presets_routes.py    # /api/presets
    data_routes.py       # /api/datasets (browse/preview generated data)
    import_routes.py     # /api/import (SQL Server / Postgres import management)
  frontend/            # React SPA (YAML editor, presets, log viewer)
```

## Thread safety — the rule that matters most

Shared mutable globals in `web/shared_state.py` (`_cfg`, `_models_cfg`, etc.) are
guarded by **`_cfg_lock`**. FastAPI serves requests concurrently, so:

- **Always acquire `_cfg_lock`** when reading or mutating these globals from a route
  handler.
- **Reads must `copy.deepcopy()` under the lock** before returning/using the value, or
  a concurrent mutation races you. (Gotcha #13.)

This is the easiest thing to get wrong when adding an endpoint that touches config.

## API versioning — every endpoint is dual-mounted

All endpoints are available at **both** `/api/...` (backward-compatible) and
`/v1/api/...` (versioned). Both hit the **same** router handlers (gotcha #12). When you
add a route to a router, it is automatically exposed under both prefixes — don't
hand-register a second copy.

## Security posture (already configured — don't weaken it)

- **CORS is an allowlist, not `*`.** `web/api.py` restricts origins to `localhost` /
  `127.0.0.1` on ports `8502` and `3000` by default; override via the `CORS_ORIGINS`
  env var (gotcha #19). Do not replace it with `allow_origins=["*"]`.
- **`SecurityHeadersMiddleware`** adds `X-Content-Type-Options`, `X-Frame-Options`,
  `X-XSS-Protection`, and `Referrer-Policy` to all responses. Keep new responses flowing
  through it.
- **Payload size limits:** the YAML and models endpoints reject bodies **> 1 MB**
  (HTTP 413) to prevent memory exhaustion (gotcha #20). If you add an endpoint that
  accepts a large body, decide deliberately whether the same limit applies.

## Config objects are Pydantic, not dicts

Route handlers read/write `cfg` (`AppConfig`) and `models_cfg` (`ModelsInnerConfig`) —
both Pydantic models. Use attribute access, and copy with `model_copy(deep=True)`. See
the **config-schema** skill for the full rules; it matters here because handlers are a
common place people accidentally treat these as plain dicts.

## Running & testing

```bash
python -m uvicorn web.api:app --port 8502     # dev server
pytest tests/test_web_api.py tests/test_web_routes.py
```

The web API/route tests require `httpx` and are **skipped** without it — install it if
you're changing endpoints so the tests actually run.
