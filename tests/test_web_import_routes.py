"""Focused contracts for the web database-import wizard."""
from __future__ import annotations

from pathlib import Path

import pytest

httpx = pytest.importorskip("httpx", reason="httpx required for web route tests")
from starlette.testclient import TestClient  # noqa: E402

from web.api import app  # noqa: E402
from web.routes import import_routes as routes  # noqa: E402


def test_postgres_command_uses_env_password_and_forwards_options(tmp_path: Path) -> None:
    request = routes.ImportRequest(
        engine="postgres",
        server="db.internal",
        port=5544,
        database="contoso",
        dataset="run",
        user="analytics",
        password="do-not-log-me",
        verify=True,
        identifier_case="snake",
        load_workers=4,
    )

    command, env = routes._build_import_command(request, tmp_path)
    rendered = " ".join(command)

    assert "run_postgres_import.py" in rendered
    assert command[command.index("--host") + 1] == "db.internal"
    assert command[command.index("--port") + 1] == "5544"
    assert command[command.index("--identifier-case") + 1] == "snake"
    assert command[command.index("--load-workers") + 1] == "4"
    assert "--no-verify" not in command
    assert "do-not-log-me" not in rendered
    assert env["PGPASSWORD"] == "do-not-log-me"


def test_postgres_command_can_skip_verification(tmp_path: Path) -> None:
    request = routes.ImportRequest(
        engine="postgres",
        server="localhost",
        database="contoso",
        dataset="run",
        user="postgres",
        verify=False,
    )

    command, _env = routes._build_import_command(request, tmp_path)

    assert "--no-verify" in command


def test_existing_sql_server_request_contract_remains_default(tmp_path: Path) -> None:
    request = routes.ImportRequest(
        server=r"HOST\SQLEXPRESS",
        database="Contoso",
        dataset="run",
        trusted=False,
        user="sa",
        password="do-not-log-me",
        apply_cci=True,
        verify=True,
    )

    command, env = routes._build_import_command(request, tmp_path)
    rendered = " ".join(command)

    assert request.engine == "sqlserver"
    assert "run_sql_server_import.py" in rendered
    assert "--apply-cci" in command
    assert "--verify" in command
    assert "--password-env" in command
    assert "do-not-log-me" not in rendered
    assert env["SYNDATA_DB_PASSWORD"] == "do-not-log-me"


def test_capabilities_are_available_on_both_api_prefixes(monkeypatch) -> None:
    monkeypatch.setattr(routes, "_postgres_available", lambda: True)

    with TestClient(app) as client:
        legacy = client.get("/api/import/capabilities")
        versioned = client.get("/v1/api/import/capabilities")

    assert legacy.status_code == 200
    assert versioned.status_code == 200
    assert legacy.json()["postgres"] == {
        "available": True,
        "driver": "psycopg",
        "error": None,
    }
    assert versioned.json() == legacy.json()


def test_postgres_start_requires_username_before_launch() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/api/import/start",
            json={
                "engine": "postgres",
                "server": "localhost",
                "database": "contoso",
                "dataset": "run",
                "user": "",
            },
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "PostgreSQL username is required."
