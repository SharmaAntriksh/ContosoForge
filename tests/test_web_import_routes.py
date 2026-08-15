"""Focused contracts for the web database-import wizard."""
from __future__ import annotations

from pathlib import Path

import pytest

httpx = pytest.importorskip("httpx", reason="httpx required for web route tests")
from starlette.testclient import TestClient  # noqa: E402

from web.api import app  # noqa: E402
from web.routes import import_routes as routes  # noqa: E402


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (
            "2026-08-15 12:42:07 PM | INFO |   Applying Columnstore Indexes (45 tables)",
            "Applying Columnstore Indexes (45 tables)",
        ),
        (
            "2026-08-15 12:42:07 PM | INFO |   Creating Indexes",
            "Creating Indexes",
        ),
        ("2026-08-15 12:42:07 PM | INFO |   Server: localhost", None),
        ("2026-08-15 12:42:07 PM | WORK |     01_create_indexes.sql", None),
        ("not an importer log line", None),
    ],
)
def test_import_status_tracks_engine_agnostic_info_phases(line: str, expected: str | None) -> None:
    assert routes._status_from_import_log(line) == expected


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


def test_import_status_exposes_current_activity() -> None:
    job = {
        "id": "job-1",
        "status": "running",
        "logs": [],
        "started": 1.0,
        "elapsed": 0,
        "exit_code": None,
        "dataset": "run",
        "database": "Contoso",
        "server": "localhost",
        "engine": "sqlserver",
        "current_status": "Applying Columnstore Indexes (45 tables)",
    }
    with routes._import_lock:
        previous = routes._current_import
        routes._current_import = job
    try:
        with TestClient(app) as client:
            response = client.get("/api/import/status")
    finally:
        with routes._import_lock:
            routes._current_import = previous

    assert response.status_code == 200
    assert response.json()["current_status"] == "Applying Columnstore Indexes (45 tables)"


def test_import_stream_carries_current_activity() -> None:
    job = {
        "id": "job-2",
        "status": "done",
        "logs": [],
        "started": 1.0,
        "ended": 3.0,
        "elapsed": 2.0,
        "exit_code": 0,
        "current_status": "Import completed",
    }
    with routes._import_lock:
        previous = routes._current_import
        routes._current_import = job
    try:
        with TestClient(app) as client:
            response = client.get("/api/import/stream")
    finally:
        with routes._import_lock:
            routes._current_import = previous

    assert response.status_code == 200
    assert '"current_status": "Import completed"' in response.text
    assert response.text.count('"type": "end"') == 1


def test_cancel_stays_non_terminal_until_import_thread_finishes(monkeypatch) -> None:
    class RunningProcess:
        @staticmethod
        def poll() -> None:
            return None

    stopped = []
    job = {
        "id": "job-cancel",
        "status": "running",
        "logs": [],
        "process": RunningProcess(),
        "started": 1.0,
        "elapsed": 0,
        "current_status": "Loading data",
    }
    monkeypatch.setattr(routes, "_stop_child", lambda current, proc: stopped.append(proc))
    with routes._import_lock:
        previous = routes._current_import
        routes._current_import = job
    try:
        with TestClient(app) as client:
            response = client.post("/api/import/cancel")
    finally:
        with routes._import_lock:
            routes._current_import = previous

    assert response.status_code == 200
    assert job["status"] == "cancelling"
    assert job["current_status"] == "Cancelling import"
    assert stopped == [job["process"]]


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
