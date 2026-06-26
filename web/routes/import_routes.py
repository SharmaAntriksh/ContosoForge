"""
web/routes/import_routes.py -- SQL Server import endpoints.

Runs the import script as a subprocess with SSE log streaming,
mirroring the generation_routes.py pattern.
"""

from __future__ import annotations

import asyncio
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
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src.tools.sql.sql_server_import import validate_tabular_login_name
from src.utils.config_helpers import int_or
from web.shared_state import REPO_ROOT, _ANSI_RE

# Spinner lines from sql_server_import.py use \r to overwrite in-place.
# In a subprocess pipe they appear as separate lines — filter them out.
_SPINNER_RE = re.compile(r"^\s*\[[-\\|/]\]\s")

router = APIRouter(prefix="/api", tags=["import"])

_IMPORT_TIMEOUT = int_or(os.environ.get("IMPORT_TIMEOUT_SECONDS"), 4 * 3600)
_DATASETS_DIR = REPO_ROOT / "generated_datasets"
_DATASETS_DIR_RESOLVED = _DATASETS_DIR.resolve()
_IMPORT_SCRIPT = REPO_ROOT / "scripts" / "sql" / "run_sql_server_import.py"


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
    server: str
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


# ---------------------------------------------------------------------------
# Import execution
# ---------------------------------------------------------------------------

def _run_import_thread(job: dict, req: ImportRequest, dataset_path: Path):
    """Run the SQL Server import script as a subprocess."""
    global _current_import
    try:

        cmd = [
            sys.executable, "-u", str(_IMPORT_SCRIPT),
            "--server", req.server,
            "--database", req.database,
            "--run-path", str(dataset_path),
        ]

        if req.trusted:
            cmd.append("--trusted")
        else:
            if req.user:
                cmd.extend(["--user", req.user])
            # Password passed via env var to avoid exposure in process listings
            if req.password:
                cmd.append("--password-env")

        if req.apply_cci:
            cmd.append("--apply-cci")

        if req.drop_pk:
            cmd.append("--drop-pk")

        if req.verify:
            cmd.append("--verify")

        # Recovery-model tuning is on by default; only forward the opt-out.
        if not req.manage_recovery:
            cmd.append("--no-recovery-management")

        if req.provision_tabular_user:
            cmd.append("--provision-tabular-user")
            if req.tabular_login:
                cmd.extend(["--tabular-login", req.tabular_login])

        if req.odbc_driver:
            cmd.extend(["--odbc-driver", req.odbc_driver])

        job["command"] = " ".join(cmd)
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        if req.password:
            env["SYNDATA_DB_PASSWORD"] = req.password
        if req.provision_tabular_user and req.tabular_password:
            env["SYNDATA_TABULAR_PASSWORD"] = req.tabular_password
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=env, cwd=str(REPO_ROOT),
        )
        job["process"] = proc

        # Timer fires while the stdout loop is still blocking — the
        # only way to enforce a wall-clock limit on the subprocess.
        timed_out = threading.Event()

        def _kill_on_timeout():
            if proc.poll() is None:
                proc.kill()
                timed_out.set()

        timer = threading.Timer(_IMPORT_TIMEOUT, _kill_on_timeout)
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

        if timed_out.is_set():
            job["logs"].append(f"ERROR: Import killed after {_IMPORT_TIMEOUT}s timeout")
        job["exit_code"] = proc.returncode
        job["status"] = "done" if proc.returncode == 0 else "failed"
    except Exception:
        logging.getLogger(__name__).exception("Import thread failed")
        job["logs"].append("ERROR: Import failed unexpectedly. Check server logs for details.")
        job["status"] = "failed"
        job["exit_code"] = -1
    finally:
        job["ended"] = time.time()
        job["elapsed"] = job["ended"] - job["started"]


@router.post("/import/start")
def start_import(req: ImportRequest):
    """Start a SQL Server import job."""
    global _current_import

    # Validate inputs
    if not req.server or not req.server.strip():
        raise HTTPException(400, "Server name is required.")
    if not req.database or not req.database.strip():
        raise HTTPException(400, "Database name is required.")
    if not req.dataset or not req.dataset.strip():
        raise HTTPException(400, "Dataset must be selected.")
    if not req.trusted and not req.user:
        raise HTTPException(400, "Username is required when not using Windows Authentication.")
    if req.provision_tabular_user:
        if not req.tabular_password:
            raise HTTPException(400, "Tabular user password is required when provisioning is enabled.")
        if req.tabular_login:
            try:
                validate_tabular_login_name(req.tabular_login)
            except ValueError as exc:
                raise HTTPException(400, str(exc))

    # Validate dataset exists and has SQL scripts (CSV format only)
    try:
        dataset_path = _safe_dataset_path(req.dataset)
    except ValueError:
        raise HTTPException(400, "Invalid dataset name.")
    if not dataset_path.is_dir():
        raise HTTPException(404, f"Dataset not found: {req.dataset}")
    sql_dir = dataset_path / "sql"
    if not sql_dir.is_dir():
        raise HTTPException(400, "Dataset has no SQL scripts. SQL import requires CSV format output.")

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
            proc.terminate()
            _current_import["status"] = "cancelled"
            _current_import["logs"].append("Import cancelled by user.")
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
    }
