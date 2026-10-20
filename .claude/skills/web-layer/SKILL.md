---
name: web-layer
description: >-
  Read before editing web/. Covers FastAPI routes and editor state, locking, dual API mounting, generation/import subprocesses and SSE, React asset builds, offline serving, CSP, and payload limits.
---

# Web Layer: FastAPI Backend + React SPA

The web UI is a FastAPI backend serving a React SPA. `api.py` is the app entry point;
routers are split by concern under `web/routes/`; mutable server state lives in
`web/shared_state.py`.

## Layout

```
web/
  api.py               # FastAPI app: mounts routers, CORS, security middleware, serves SPA
  shared_state.py      # mutable editor state + _cfg_lock and generation job state
  process_control.py   # subprocess groups / Windows Job Objects and graceful stop
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
  `X-XSS-Protection`, `Referrer-Policy`, and Content Security Policy to all responses. Keep new responses flowing
  through it.
- **Payload size limits:** the YAML and models endpoints reject bodies **> 1 MB**
  (HTTP 413) to prevent memory exhaustion (gotcha #20). If you add an endpoint that
  accepts a large body, decide deliberately whether the same limit applies.

## Editor state and generation snapshots

`web.shared_state._cfg` starts as an `AppConfig`. `_models_cfg` is a **raw root
YAML dict**, including the `models` key; models routes validate it using
`ModelsConfig.from_raw_dict()` but retain the dict for editing. Do not assume
both web globals are `ModelsInnerConfig` instances. Typed pipeline config after
loading is a separate boundary; see the config-schema skill.

Copy editor state while holding `_cfg_lock` (`copy.deepcopy()` handles either
representation). Do not import a mutable singleton by value and then rebind it
in a route: use `import web.shared_state as _state` and mutate `_state` under its
lock. Use `_job_lock` for generation state and `_import_lock` in import routes;
copy a job/log snapshot under the lock, then stream outside it.

Quick Start, presets, and editor changes are kept in memory. Generation takes
config/models snapshots and writes temporary configs for its child process;
repo YAML files are not implicitly saved. Preserve this distinction in API
changes and UI text.

## Frontend source and committed assets

FastAPI serves `web/frontend/dist/`, not JSX directly. React and CodeMirror are
bundled locally with esbuild; the end user needs no Node.js or CDN access.
After source changes, run from `web/frontend/`:

```powershell
npm ci
npm run build
npm run build:check
```

Commit the resulting content-hashed assets, index, and manifest with the source.
Do not hand-edit `dist/`. `index.template.html` and `theme-init.js` participate in
the build; keep the CSP compatible with local scripts (no executable inline JS
or new remote runtime dependencies). See `web/frontend/README.md`.

## Generation/import subprocesses

Use `web/process_control.py` for process groups, Windows Job Objects, and stop
handling. Killing only the launcher can orphan generation workers. SQL Server
imports may temporarily change recovery mode; graceful stop must allow the
importer's cleanup to run before the hard-kill fallback. Preserve interruption
hints and bounded, cursor-based SSE logs; don't accumulate every log line or
force the frontend to interpret raw terminal spinner updates.

Import requests are built in `import_routes.py` and executed through
`scripts/sql/run_*_import.py`. Test the request/argv/password-env mapping when
adding an importer option, as well as its frontend control. Both Python drivers
are default dependencies; the SQL Server system ODBC driver is separate.

## Running & testing

Use `.venv/Scripts/python.exe -m uvicorn web.api:app --port 8502` for the server
(POSIX uses `.venv/bin/python`). Install test tooling with
`uv sync --locked --extra dev`; without `httpx`, API tests may skip.

Choose from `test_web_api.py`, `test_web_routes.py`, `test_web_import_routes.py`,
`test_web_data_preview.py`, and `test_web_frontend.py` under `tests/` according to
what changed. For frontend changes, also run `npm run build:check`.
`scripts/validate_web_release.py` generates small release datasets in an archive
of **committed HEAD**; it does not validate an uncommitted source edit.
