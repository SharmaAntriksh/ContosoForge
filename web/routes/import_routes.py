"""
web/routes/import_routes.py -- database import endpoints.

Runs the SQL Server or PostgreSQL importer with SSE log streaming,
mirroring the generation_routes.py pattern.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json as _json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from src.tools.sql.sql_server_import import validate_tabular_login_name
from src.utils.config_helpers import int_or
from web.process_control import (
    attach_process_tree,
    process_group_popen_kwargs,
    release_process_tree,
    request_process_tree_stop,
)
from web.shared_state import REPO_ROOT, _ANSI_RE

# Spinner lines from sql_server_import.py use \r to overwrite in-place.
# In a subprocess pipe they appear as separate lines — filter them out.
_SPINNER_RE = re.compile(r"^\s*\[[-\\|/]\]\s")

router = APIRouter(prefix="/api", tags=["import"])

_IMPORT_TIMEOUT = int_or(os.environ.get("IMPORT_TIMEOUT_SECONDS"), 4 * 3600)
# Grace window between asking the child to stop and hard-killing it, so its
# cleanup (the FULL<->BULK_LOGGED recovery-model revert in a `finally`) can run.
_KILL_GRACE = int_or(os.environ.get("IMPORT_KILL_GRACE_SECONDS"), 30)
_DATASETS_DIR = REPO_ROOT / "generated_datasets"
_DATASETS_DIR_RESOLVED = _DATASETS_DIR.resolve()
_IMPORT_SCRIPTS = {
    "sqlserver": REPO_ROOT / "scripts" / "sql" / "run_sql_server_import.py",
    "postgres": REPO_ROOT / "scripts" / "sql" / "run_postgres_import.py",
}

# Appended to the job log whenever an import is interrupted (timeout/cancel),
# because a hard stop can leave the target DB half-populated and (on SQL Server)
# in BULK_LOGGED recovery with a broken log-backup chain.
_SQLSERVER_INTERRUPT_HINT = (
    "NOTE: an interrupted import can leave the target database partially "
    "populated — drop it before retrying (the importer refuses to reuse an "
    "existing database). On SQL Server it may also be left in BULK_LOGGED "
    "recovery; re-run sql/sqlserver/load/99_*_finish_load.sql against it to restore the "
    "recovery model and then BACKUP LOG to restart the log-backup chain."
)
_POSTGRES_INTERRUPT_HINT = (
    "NOTE: an interrupted import can leave the target PostgreSQL database "
    "partially populated. Drop it before retrying because the importer refuses "
    "to reuse an existing database."
)


def _stop_child(job: dict, proc: subprocess.Popen, *, grace: int = _KILL_GRACE) -> None:
    """Graceful stop, then hard-kill if the child overstays the grace window.

    Arms a background timer that force-kills only if the child is still alive
    after ``grace`` seconds; the reader thread cancels it once the child exits
    on its own. Safe to call from both the timeout timer and the cancel handler.
    """
    if proc.poll() is not None:
        return
    timer = request_process_tree_stop(proc, grace=grace)
    if timer is not None:
        job.setdefault("_kill_timers", []).append(timer)


def _safe_dataset_path(name: str) -> Path:
    """Resolve dataset name to a path, rejecting traversal attempts."""
    resolved = (_DATASETS_DIR / name).resolve()
    if not resolved.is_relative_to(_DATASETS_DIR_RESOLVED):
        raise ValueError("Invalid dataset name")
    return resolved

# ---------------------------------------------------------------------------
# Job state (single import at a time)
# ---------------------------------------------------------------------------

_import_lock = threading.Lock()
_current_import: dict | None = None


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class ImportRequest(BaseModel):
    engine: Literal["sqlserver", "postgres"] = "sqlserver"
    server: str
    port: int = Field(default=5432, ge=1, le=65535)
    database: str
    dataset: str                          # folder name in generated_datasets/
    trusted: bool = True
    user: Optional[str] = None
    password: Optional[str] = None
    apply_cci: bool = False
    drop_pk: bool = False
    verify: bool = False
    manage_recovery: bool = True
    odbc_driver: Optional[str] = None
    provision_tabular_user: bool = False
    tabular_login: Optional[str] = None
    tabular_password: Optional[str] = None
    identifier_case: Literal["pascal", "snake"] = "pascal"
    load_workers: int = Field(default=1, ge=1, le=64)


# ---------------------------------------------------------------------------
# SQL Server instance discovery
# ---------------------------------------------------------------------------

def _discover_local_instances() -> list[dict]:
    """Discover local SQL Server instances via Windows registry."""
    results = []
    if sys.platform != "win32":
        return results
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Microsoft SQL Server\Instance Names\SQL",
        ) as key:
            hostname = os.environ.get("COMPUTERNAME", "localhost")
            i = 0
            while True:
                try:
                    name, _val, _ = winreg.EnumValue(key, i)
                    server = hostname if name.upper() == "MSSQLSERVER" else f"{hostname}\\{name}"
                    results.append({"server": server, "source": "local"})
                    i += 1
                except OSError:
                    break
    except OSError:
        pass
    return results


def _discover_network_instances(timeout: float = 2.0) -> list[dict]:
    """Discover SQL Server instances on the network via UDP broadcast."""
    import socket
    results = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(b"\x02", ("<broadcast>", 1434))
            seen = set()
            while True:
                try:
                    data, addr = sock.recvfrom(4096)
                    info = data[3:].decode("ascii", errors="replace")
                    # Parse "ServerName;FOO;InstanceName;BAR;..." pairs
                    parts = info.strip(";").split(";")
                    pairs = dict(zip(parts[0::2], parts[1::2]))
                    srv_name = pairs.get("ServerName", addr[0])
                    inst_name = pairs.get("InstanceName", "")
                    version = pairs.get("Version", "")
                    server = srv_name if inst_name.upper() == "MSSQLSERVER" else f"{srv_name}\\{inst_name}"
                    if server not in seen:
                        seen.add(server)
                        results.append({"server": server, "source": "network", "version": version})
                except socket.timeout:
                    break
    except OSError:
        pass
    return results


@router.get("/import/servers")
def list_sql_servers():
    """Discover available SQL Server instances (local + network)."""
    local = _discover_local_instances()
    network = _discover_network_instances(timeout=2.0)
    # Merge: prefer local entries, add network-only ones
    seen = {s["server"].upper() for s in local}
    merged = list(local)
    for s in network:
        if s["server"].upper() not in seen:
            merged.append(s)
        else:
            # Enrich local entry with version from network discovery
            for m in merged:
                if m["server"].upper() == s["server"].upper() and s.get("version"):
                    m["version"] = s["version"]
    return {"servers": merged}


# ---------------------------------------------------------------------------
# ODBC driver detection
# ---------------------------------------------------------------------------

@router.get("/import/drivers")
def list_odbc_drivers():
    """List available SQL Server ODBC drivers on this machine."""
    try:
        import pyodbc
        all_drivers = pyodbc.drivers()
        sql_drivers = [d for d in all_drivers if "sql server" in d.lower()]
        return {"available": True, "drivers": sql_drivers}
    except ImportError:
        return {"available": False, "drivers": [], "error": "pyodbc not installed"}
    except Exception as exc:
        return {"available": False, "drivers": [], "error": str(exc)}


def _postgres_available() -> bool:
    """Return whether the optional PostgreSQL driver is installed."""
    return importlib.util.find_spec("psycopg") is not None


@router.get("/import/capabilities")
def import_capabilities():
    """Report local client dependencies used by each import engine."""
    postgres_available = _postgres_available()
    return {
        "sqlserver": {
            "driver": "pyodbc",
        },
        "postgres": {
            "available": postgres_available,
            "driver": "psycopg",
            "error": None if postgres_available else (
                "PostgreSQL support is not installed. Run "
                "`uv sync --extra postgres` and restart the web UI."
            ),
        },
    }


# ---------------------------------------------------------------------------
# Import execution
# ---------------------------------------------------------------------------

def _build_import_command(
    req: ImportRequest,
    dataset_path: Path,
) -> tuple[list[str], dict[str, str]]:
    """Build an engine-specific importer command and password-safe environment."""
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}

    if req.engine == "postgres":
        cmd = [
            sys.executable,
            "-u",
            str(_IMPORT_SCRIPTS["postgres"]),
            "--host",
            req.server,
            "--port",
            str(req.port),
            "--database",
            req.database,
            "--user",
            req.user or "postgres",
            "--run-path",
            str(dataset_path),
            "--identifier-case",
            req.identifier_case,
            "--load-workers",
            str(req.load_workers),
        ]
        if not req.verify:
            cmd.append("--no-verify")
        if req.password:
            env["PGPASSWORD"] = req.password
        return cmd, env

    cmd = [
        sys.executable,
        "-u",
        str(_IMPORT_SCRIPTS["sqlserver"]),
        "--server",
        req.server,
        "--database",
        req.database,
        "--run-path",
        str(dataset_path),
    ]

    if req.trusted:
        cmd.append("--trusted")
    else:
        if req.user:
            cmd.extend(["--user", req.user])
        # Password passed via env var to avoid exposure in process listings.
        if req.password:
            cmd.append("--password-env")

    if req.apply_cci:
        cmd.append("--apply-cci")
    if req.drop_pk:
        cmd.append("--drop-pk")
    if req.verify:
        cmd.append("--verify")
    if not req.manage_recovery:
        cmd.append("--no-recovery-management")
    if req.provision_tabular_user:
        cmd.append("--provision-tabular-user")
        if req.tabular_login:
            cmd.extend(["--tabular-login", req.tabular_login])
    if req.odbc_driver:
        cmd.extend(["--odbc-driver", req.odbc_driver])

    if req.password:
        env["SYNDATA_DB_PASSWORD"] = req.password
    if req.provision_tabular_user and req.tabular_password:
        env["SYNDATA_TABULAR_PASSWORD"] = req.tabular_password
    return cmd, env


def _run_import_thread(job: dict, req: ImportRequest, dataset_path: Path):
    """Run the selected database importer as a subprocess."""
    global _current_import
    proc = None
    try:
        cmd, env = _build_import_command(req, dataset_path)
        job["command"] = " ".join(cmd)
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=env, cwd=str(REPO_ROOT),
            **process_group_popen_kwargs(),
        )
        attach_process_tree(proc)
        job["process"] = proc

        # Timer fires while the stdout loop is still blocking — the
        # only way to enforce a wall-clock limit on the subprocess. Try a
        # graceful stop first (so the child's recovery-model revert runs),
        # then hard-kill after a grace window.
        timed_out = threading.Event()

        def _on_timeout():
            if proc.poll() is None:
                timed_out.set()
                _stop_child(job, proc)

        timer = threading.Timer(_IMPORT_TIMEOUT, _on_timeout)
        timer.start()
        try:
            for line in proc.stdout:
                clean = _ANSI_RE.sub("", line.rstrip())
                if not clean or _SPINNER_RE.match(clean):
                    continue
                job["logs"].append(clean)
            proc.wait()
        finally:
            timer.cancel()
            for kt in job.get("_kill_timers", []):
                kt.cancel()

        if timed_out.is_set():
            job["logs"].append(f"ERROR: Import killed after {_IMPORT_TIMEOUT}s timeout")
        # Any interruption (timeout or user cancel) can orphan the DB / leave
        # BULK_LOGGED recovery — spell out the recovery steps once the run ends.
        if timed_out.is_set() or job.get("status") == "cancelled":
            hint = (
                _POSTGRES_INTERRUPT_HINT
                if req.engine == "postgres"
                else _SQLSERVER_INTERRUPT_HINT
            )
            job["logs"].append(hint)
        job["exit_code"] = proc.returncode
        # Preserve a user-initiated cancel: terminate() makes the process exit
        # non-zero, which would otherwise be reported as "failed".
        if job["status"] != "cancelled":
            job["status"] = "done" if proc.returncode == 0 else "failed"
    except Exception:
        logging.getLogger(__name__).exception("Import thread failed")
        job["logs"].append("ERROR: Import failed unexpectedly. Check server logs for details.")
        job["status"] = "failed"
        job["exit_code"] = -1
    finally:
        if proc is not None:
            release_process_tree(proc)
        job["ended"] = time.time()
        job["elapsed"] = job["ended"] - job["started"]


@router.post("/import/start")
def start_import(req: ImportRequest):
    """Start a SQL Server or PostgreSQL import job."""
    global _current_import

    # Validate inputs
    if not req.server or not req.server.strip():
        raise HTTPException(400, "Server name is required.")
    if not req.database or not req.database.strip():
        raise HTTPException(400, "Database name is required.")
    if not req.dataset or not req.dataset.strip():
        raise HTTPException(400, "Dataset must be selected.")
    if req.engine == "sqlserver" and not req.trusted and not req.user:
        raise HTTPException(400, "Username is required when not using Windows Authentication.")
    if req.engine == "sqlserver" and req.provision_tabular_user:
        if not req.tabular_password:
            raise HTTPException(400, "Tabular user password is required when provisioning is enabled.")
        if req.tabular_login:
            try:
                validate_tabular_login_name(req.tabular_login)
            except ValueError as exc:
                raise HTTPException(400, str(exc))
    if req.engine == "postgres":
        if not req.user or not req.user.strip():
            raise HTTPException(400, "PostgreSQL username is required.")
        if not _postgres_available():
            raise HTTPException(
                400,
                "PostgreSQL support is not installed. Run "
                "`uv sync --extra postgres` and restart the web UI.",
            )

    # Validate dataset exists and has SQL scripts (CSV format only)
    try:
        dataset_path = _safe_dataset_path(req.dataset)
    except ValueError:
        raise HTTPException(400, "Invalid dataset name.")
    if not dataset_path.is_dir():
        raise HTTPException(404, f"Dataset not found: {req.dataset}")
    sql_dir = dataset_path / "sql" / req.engine
    if not sql_dir.is_dir():
        engine_name = "PostgreSQL" if req.engine == "postgres" else "SQL Server"
        raise HTTPException(
            400,
            f"Dataset has no {engine_name} SQL scripts. Database import requires CSV format output.",
        )

    with _import_lock:
        if _current_import and _current_import["status"] == "running":
            raise HTTPException(409, "An import is already running.")
        job = {
            "id": str(uuid.uuid4())[:8],
            "status": "running",
            "logs": [],
            "process": None,
            "exit_code": None,
            "started": time.time(),
            "ended": None,
            "elapsed": 0,
            "command": "",
            "dataset": req.dataset,
            "database": req.database,
            "server": req.server,
            "engine": req.engine,
        }
        _current_import = job

    t = threading.Thread(target=_run_import_thread, args=(job, req, dataset_path), daemon=True)
    t.start()
    return {"ok": True, "job_id": job["id"]}


@router.get("/import/stream")
async def stream_import_logs():
    """SSE stream for import progress."""
    async def event_stream():
        idx = 0
        while True:
            with _import_lock:
                job = _current_import
            if job is None:
                yield f"data: {_json.dumps({'type': 'idle'})}\n\n"
                break
            logs = list(job["logs"])
            for line in logs[idx:]:
                yield f"data: {_json.dumps({'type': 'log', 'line': line})}\n\n"
            idx = len(logs)
            elapsed = time.time() - job["started"] if job["status"] == "running" else job.get("elapsed", 0)
            yield f"data: {_json.dumps({'type': 'status', 'status': job['status'], 'elapsed': round(elapsed, 1)})}\n\n"
            if job["status"] in ("done", "failed", "cancelled"):
                yield f"data: {_json.dumps({'type': 'end', 'status': job['status'], 'exit_code': job.get('exit_code'), 'elapsed': round(elapsed, 1)})}\n\n"
                break
            await asyncio.sleep(0.3)
    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/import/cancel")
def cancel_import():
    """Cancel a running import."""
    global _current_import
    with _import_lock:
        if not _current_import or _current_import["status"] != "running":
            raise HTTPException(400, "No running import to cancel.")
        proc = _current_import.get("process")
        if proc and proc.poll() is None:
            _current_import["status"] = "cancelled"
            _current_import["logs"].append("Import cancelled by user.")
            # Graceful stop first (lets the child revert the recovery model),
            # then hard-kill after the grace window if it doesn't exit.
            _stop_child(_current_import, proc)
            return {"ok": True}
    raise HTTPException(400, "Could not cancel.")


@router.get("/import/status")
def import_status():
    """Get current import job status."""
    with _import_lock:
        job = _current_import
    if not job:
        return {"status": "idle"}
    elapsed = time.time() - job["started"] if job["status"] == "running" else job.get("elapsed", 0)
    return {
        "id": job["id"],
        "status": job["status"],
        "elapsed": round(elapsed, 1),
        "exit_code": job.get("exit_code"),
        "log_count": len(job["logs"]),
        "dataset": job.get("dataset"),
        "database": job.get("database"),
        "server": job.get("server"),
        "engine": job.get("engine", "sqlserver"),
    }
