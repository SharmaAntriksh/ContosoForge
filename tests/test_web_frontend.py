"""Small executable contract tests for the browser's shared API client."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = REPO_ROOT / "web" / "frontend"
DIST = FRONTEND / "dist"
API_CLIENT = FRONTEND / "api-client.js"


@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [
        (400, {"detail": "Bad request"}, "Bad request"),
        (409, {"detail": "A pipeline is already running."}, "already running"),
        (
            422,
            {
                "detail": {
                    "field": "salesRows",
                    "message": "Expected an integer value.",
                },
            },
            "salesRows: Expected an integer value.",
        ),
        (500, None, "500 Internal Server Error"),
    ],
)
def test_api_client_rejects_http_errors(status, payload, expected):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for frontend contract tests")

    content_type = "application/json" if payload is not None else "text/html"
    script = f"""
global.window = global;
global.fetch = async () => ({{
  ok: false,
  status: {status},
  statusText: {json.dumps("Internal Server Error" if status == 500 else "Error")},
  headers: {{get: () => {json.dumps(content_type)}}},
  json: async () => ({json.dumps(payload)}),
  text: async () => "",
}});
(async () => {{
  await import({json.dumps(API_CLIENT.as_uri())});
  try {{
    await global.apiRequest("/test");
    process.stderr.write("request unexpectedly succeeded");
    process.exit(2);
  }} catch (error) {{
    if (!error.message.includes({json.dumps(expected)})) {{
      process.stderr.write(error.message);
      process.exit(3);
    }}
  }}
}})().catch((error) => {{
  process.stderr.write(error.stack || error.message);
  process.exit(4);
}});
"""
    result = subprocess.run(
        [node, "-e", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_all_frontend_modules_use_shared_api_client():
    build = (FRONTEND / "build.mjs").read_text(encoding="utf-8")
    assert '"api-client.js"' in build

    for name in ("app.jsx", "data-preview.jsx", "database-import.jsx"):
        source = (FRONTEND / name).read_text(encoding="utf-8")
        assert "window.apiRequest" in source
        assert "fetch(" not in source


def test_committed_production_frontend_is_self_contained_and_current():
    index = (DIST / "index.html").read_text(encoding="utf-8")
    manifest = json.loads((DIST / "asset-manifest.json").read_text(encoding="utf-8"))

    assert "http://" not in index
    assert "https://" not in index
    assert "text/babel" not in index.lower()
    assert "babel" not in index.lower()
    assert "fonts.googleapis.com" not in index
    assert "unpkg.com" not in index
    assert "cdnjs.cloudflare.com" not in index

    script_tags = re.findall(r"<script([^>]*)>(.*?)</script>", index, re.DOTALL)
    assert script_tags
    assert all("src=" in attributes and not body.strip() for attributes, body in script_tags)

    for asset in manifest["assets"].values():
        assert re.search(r"-[A-Z0-9]{8,}\.(?:css|js)$", asset, re.IGNORECASE)
        assert (DIST / asset).is_file()
        assert f"/static/{asset}" in index

    current_hashes = {
        name: hashlib.sha256((FRONTEND / name).read_bytes()).hexdigest()
        for name in manifest["sources"]
    }
    assert current_hashes == manifest["sources"]

    digest = hashlib.sha256()
    for name, source_hash in sorted(current_hashes.items()):
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(source_hash.encode())
        digest.update(b"\0")
    assert digest.hexdigest() == manifest["sourceDigest"]


def test_frontend_build_pins_local_runtime_dependencies():
    package = json.loads((FRONTEND / "package.json").read_text(encoding="utf-8"))
    lockfile = json.loads((FRONTEND / "package-lock.json").read_text(encoding="utf-8"))
    build = (FRONTEND / "build.mjs").read_text(encoding="utf-8")
    styles = (FRONTEND / "styles.css").read_text(encoding="utf-8")

    assert package["private"] is True
    assert package["scripts"]["build"] == "node build.mjs"
    assert {"react", "react-dom", "codemirror"} <= package["dependencies"].keys()
    assert lockfile["lockfileVersion"] >= 3
    assert 'import {build} from "esbuild"' in build
    assert '"codemirror/mode/yaml/yaml.js"' in build
    assert "fonts.googleapis.com" not in styles
    assert "--sans:'Segoe UI Variable','Segoe UI',system-ui" in styles
    assert "--mono:Consolas,'Cascadia Code'" in styles
    assert ".import-dataset-meta" in styles
    assert "font:600 10.5px var(--sans);font-variant-numeric:tabular-nums" in styles


def test_generation_ui_restores_server_state_without_fallback_config():
    source = (REPO_ROOT / "web" / "frontend" / "app.jsx").read_text(
        encoding="utf-8",
    )

    assert 'window.apiRequest(API + "/generate/status")' in source
    assert "connectGenerationStream(status.elapsed || 0)" in source
    assert "No fallback settings have been substituted." in source
    assert "setCfg({seed:" not in source
    assert "disabled={errors.length > 0 || !configSynced}" in source


def test_generation_logs_are_bounded_and_resume_by_sequence():
    source = (FRONTEND / "app.jsx").read_text(encoding="utf-8")

    assert "const BROWSER_LOG_LIMIT = 2000" in source
    assert "function appendBoundedLogs(previous, additions)" in source
    assert "BROWSER_LOG_TRUNCATION_NOTICE" in source
    assert "logCursorRef.current" in source
    assert "/generate/stream?after=" in source
    assert "data.type === \"truncated\"" in source
    assert "data.sequence || event.lastEventId" in source


def test_generation_terminal_status_is_preserved_for_accessible_feedback():
    app = (FRONTEND / "app.jsx").read_text(encoding="utf-8")
    logs = (FRONTEND / "log-viewer.jsx").read_text(encoding="utf-8")

    assert "setRunStatus(data.status)" in app
    assert "setRunStatus(status.status)" in app
    assert 'status==="cancelled"' in logs
    assert '"Cancelled"' in logs
    assert 'status==="timed_out"' in logs
    assert '"Timed out"' in logs
    assert 'status==="failed"' in logs
    assert '"Failed"' in logs
    assert "aria-valuetext={statusLabel}" in logs


def test_application_navigation_supports_escape_and_focus_return():
    source = (FRONTEND / "app.jsx").read_text(encoding="utf-8")

    assert 'event.key !== "Escape"' in source
    assert "sidebarToggleRef.current?.focus()" in source
    assert 'aria-label="Application navigation"' in source
    assert 'aria-controls="application-navigation"' in source
    assert "<span>Data Generator</span>" in source
    assert 'aria-pressed={page === "main"} onClick={() => setPage("main")}' in source


def test_quick_start_keeps_five_decisions_and_advanced_setup():
    app_source = (REPO_ROOT / "web" / "frontend" / "app.jsx").read_text(
        encoding="utf-8",
    )
    guided_source = (
        REPO_ROOT / "web" / "frontend" / "quick-start.jsx"
    ).read_text(encoding="utf-8")

    assert '"Quick Start"' in app_source
    assert '"Advanced Setup"' in app_source
    assert "<QuickStart" in app_source
    assert 'API + "/presets/guided"' in app_source
    assert 'const steps = ["Size", "Dates", "Business story", "Output", "Extra tables", "Review"]' in guided_source
    assert "Nothing is written to config.yaml or models.yaml." in guided_source
    assert "manual year-level factors" in guided_source
    assert "fetch(" not in guided_source


def test_quick_start_exposes_every_supported_trend():
    source = (
        REPO_ROOT / "web" / "frontend" / "quick-start.jsx"
    ).read_text(encoding="utf-8")
    expected = {
        "steady-growth",
        "strong-growth",
        "gradual-growth",
        "hockey-stick",
        "decline",
        "new-market-entry",
        "slow-decline",
        "boom-and-bust",
        "recession-recovery",
        "seasonal-dominant",
        "seasonal-with-growth",
        "plateau",
        "volatile",
        "double-dip",
        "stagnation",
    }

    assert all(f'value: "{trend}"' in source for trend in expected)


def test_shared_controls_have_accessibility_contracts():
    frontend = REPO_ROOT / "web" / "frontend"
    components = (frontend / "components.jsx").read_text(encoding="utf-8")
    app = (frontend / "app.jsx").read_text(encoding="utf-8")
    logs = (frontend / "log-viewer.jsx").read_text(encoding="utf-8")

    assert "htmlFor={controlId}" in components
    assert 'className="accessible-check-input"' in components
    assert 'type="checkbox"' in components
    assert "aria-expanded={open}" in components
    assert "aria-controls={panelId}" in components
    assert 'data-section-panel="true"' in components
    assert 'className="skip-link"' in app
    assert 'id="validation-summary"' in app
    assert "revealAndFocus(targetId)" in app
    assert 'role="log"' in logs
    assert "aria-expanded={!isCollapsed}" in logs


def test_specialized_forms_expose_stable_names_and_help():
    frontend = REPO_ROOT / "web" / "frontend"
    guided = (frontend / "quick-start.jsx").read_text(encoding="utf-8")
    sql_import = (frontend / "database-import.jsx").read_text(encoding="utf-8")
    yaml_editor = (frontend / "yaml-editor.jsx").read_text(encoding="utf-8")

    assert 'htmlFor="guided-start-date"' in guided
    assert 'id="guided-end-date"' in guided
    assert 'aria-describedby={draft.endDate < draft.startDate' in guided
    assert 'htmlFor="sql-database"' in sql_import
    assert 'describedBy="sql-verify-help"' in sql_import
    assert 'aria-label="Database engine"' in sql_import
    assert 'aria-label="Database import progress"' in sql_import
    assert 'htmlFor="pg-host"' in sql_import
    assert 'htmlFor="pg-port"' in sql_import
    assert 'htmlFor="pg-identifier-case"' in sql_import
    assert 'describedBy="pg-verify-help"' in sql_import
    assert "Import to {engineName}" in sql_import
    assert 'setAttribute("aria-label",`${filename} YAML editor`)' in yaml_editor
    assert '"Esc":()=>editorExitRef.current?.focus()' in yaml_editor
    assert "Esc exits editor" in yaml_editor
    assert 'role="alert"' in yaml_editor


def test_responsive_layout_keeps_mobile_controls_operable():
    frontend = REPO_ROOT / "web" / "frontend"
    styles = (frontend / "styles.css").read_text(encoding="utf-8")
    app = (frontend / "app.jsx").read_text(encoding="utf-8")
    guided = (frontend / "quick-start.jsx").read_text(encoding="utf-8")
    preview = (frontend / "data-preview.jsx").read_text(encoding="utf-8")

    assert "@media(max-width:900px)" in styles
    assert "@media(max-width:640px)" in styles
    assert ".generation-action{width:100%;min-height:42px}" in styles
    assert ".app-sidebar.is-closed" in styles
    assert 'className="generation-dock"' in app
    assert 'className="generation-action"' in app
    assert 'className="guided-date-grid"' in guided
    assert 'className="dataset-scroll"' in preview


def test_data_preview_supersedes_stale_requests_and_formats_page_numbers():
    source = (FRONTEND / "data-preview.jsx").read_text(encoding="utf-8")

    assert "const controller = new AbortController()" in source
    assert "{signal: controller.signal}" in source
    assert "return () => controller.abort()" in source
    assert "if (controller.signal.aborted) return" in source
    assert "currentPage.toLocaleString()" in source
    assert "totalPages.toLocaleString()" in source
    assert 'className="dataset-list"' in source
    assert 'maxHeight: 300, overflowY: "auto"' not in source
    assert "selectedDataset && !datasetExpanded && !tablesLoading" in source
    assert "selectedDataset && !datasetExpanded && selectedTable" in source
