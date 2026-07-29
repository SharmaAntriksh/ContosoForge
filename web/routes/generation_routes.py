"""
web/routes/generation_routes.py -- /api/generate and /api/validate endpoints.
"""

from __future__ import annotations

import asyncio
import copy
import json as _json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src.utils.config_helpers import int_or
from web.process_control import (
    attach_process_tree,
    cancel_stop_timers,
    force_kill_process_tree,
    process_group_popen_kwargs,
    release_process_tree,
    request_process_tree_stop,
)
from web.shared_state import (
    REPO_ROOT,
    _ANSI_RE,
    _g,
    _job_lock,
    cfg_to_dict,
    write_yaml_secure,
)
import web.shared_state as _state

router = APIRouter(prefix="/api", tags=["generation"])

_PIPELINE_TIMEOUT = int_or(os.environ.get("PIPELINE_TIMEOUT_SECONDS"), 4 * 3600)
_PIPELINE_KILL_GRACE = int_or(
    os.environ.get("PIPELINE_KILL_GRACE_SECONDS"),
    10,
)
_LOG_RETENTION = max(
    100,
    int_or(os.environ.get("WEB_LOG_RETENTION_LINES"), 5_000),
)
_VALID_ONLY = {"dimensions", "sales"}
_VALID_REGEN = {"all", "products", "customers", "stores", "employees", "dates",
                "geography", "currency", "exchange_rates", "promotions", "suppliers",
                "return_reasons", "subscriptions", "lookups", "time"}


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class GenerateRequest(BaseModel):
    clean: bool = False
    only: Optional[str] = None
    regen_dimensions: Optional[List[str]] = None


class BoundedLogBuffer(list[str]):
    """List-compatible log window with stable monotonically increasing IDs."""

    def __init__(self, max_items: int):
        super().__init__()
        if max_items < 1:
            raise ValueError("max_items must be positive")
        self.max_items = max_items
        self.first_sequence = 1
        self.next_sequence = 1
        self.dropped = 0

    @property
    def last_sequence(self) -> int:
        return self.next_sequence - 1

    def append(self, line: str) -> None:
        super().append(line)
        self.next_sequence += 1
        overflow = len(self) - self.max_items
        if overflow > 0:
            del self[:overflow]
            self.first_sequence += overflow
            self.dropped += overflow

    def entries_after(self, sequence: int) -> list[tuple[int, str]]:
        first_requested = max(sequence + 1, self.first_sequence)
        start_index = max(0, first_requested - self.first_sequence)
        return [
            (self.first_sequence + index, line)
            for index, line in enumerate(self[start_index:], start=start_index)
        ]


def _new_log_buffer(max_items: int = _LOG_RETENTION) -> BoundedLogBuffer:
    return BoundedLogBuffer(max_items)


def _append_job_log(job: dict, line: str) -> None:
    """Append under ``_job_lock`` while retaining legacy list test fixtures."""
    job["logs"].append(line)


def _log_snapshot_after(job: dict, sequence: int) -> dict:
    """Copy only the retained entries newer than ``sequence``."""
    logs = job["logs"]
    if isinstance(logs, BoundedLogBuffer):
        first_sequence = logs.first_sequence
        last_sequence = logs.last_sequence
        dropped = logs.dropped
        entries = logs.entries_after(sequence)
    else:
        first_sequence = 1
        last_sequence = len(logs)
        dropped = 0
        start = max(0, sequence)
        entries = list(enumerate(logs[start:], start=start + 1))
    return {
        "entries": entries,
        "first_sequence": first_sequence,
        "last_sequence": last_sequence,
        "retained": len(logs),
        "dropped": dropped,
        "missing_after_cursor": max(0, first_sequence - sequence - 1),
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

@router.get("/validate")
def validate_config():
    with _state._cfg_lock:
        cfg = copy.deepcopy(_state._cfg)
    errors: List[str] = []
    warnings: List[str] = []

    defaults = _g(cfg, "defaults", "dates", default={})
    start = str(getattr(defaults, "start", ""))
    end = str(getattr(defaults, "end", ""))
    if start and end and end < start:
        errors.append("End date must be after start date.")

    sales = _g(cfg, "sales", default={})
    rows = int(getattr(sales, "total_rows", 0))
    if rows <= 0:
        errors.append("Total rows must be greater than zero.")
    chunk = int(getattr(sales, "chunk_size", 0))
    if chunk > rows > 0:
        warnings.append("Chunk size exceeds total rows.")
    fmt = getattr(sales, "file_format", "parquet")
    if fmt == "csv" and rows > 5_000_000:
        warnings.append("Large CSV outputs can be slow. Consider parquet.")

    if getattr(sales, "skip_order_cols", False):
        warnings.append("Order columns skipped — Returns will not be generated.")

    customers = int(_g(cfg, "customers", "total_customers", default=0))
    products = int(_g(cfg, "products", "num_products", default=0))
    if customers > rows > 0:
        warnings.append(f"Customers ({customers:,}) exceed sales rows.")
    if products > rows > 0:
        warnings.append(f"Products ({products:,}) exceed sales rows.")

    pricing = _g(cfg, "products", "pricing", "base", default={})
    lo = float(_g(pricing, "min_unit_price", default=10))
    hi = float(_g(pricing, "max_unit_price", default=5000))
    if hi <= lo:
        errors.append("Max unit price must exceed min unit price.")

    returns = _g(cfg, "returns", default={})
    if getattr(returns, "enabled", False):
        rr = float(getattr(returns, "return_rate", 0))
        if rr < 0 or rr > 1:
            errors.append("Return rate must be between 0 and 1.")

    cust = _g(cfg, "customers", default={})
    rS = float(getattr(cust, "pct_india", 0)) + float(getattr(cust, "pct_us", 0)) + float(getattr(cust, "pct_eu", 0)) + float(getattr(cust, "pct_asia", 0))
    if rS <= 0:
        warnings.append("Customer regional percentages sum to 0.")

    ret = _g(cfg, "returns", default={})
    if getattr(ret, "enabled", False):
        mn = int(getattr(ret, "min_days_after_sale", 1))
        mx = int(getattr(ret, "max_days_after_sale", 60))
        if mx <= mn:
            warnings.append("Return max days should exceed min days.")

    return {"errors": errors, "warnings": warnings}


# ---------------------------------------------------------------------------
# Generate (subprocess + SSE)
# ---------------------------------------------------------------------------

def _build_pipeline_command(
    cfg_path: Path,
    models_path: Path,
    req: GenerateRequest,
) -> list[str]:
    """Build the exact pipeline command without starting a process."""
    cmd = [
        sys.executable, "-u", str(REPO_ROOT / "main.py"),
        "--config", str(cfg_path),
        "--models-config", str(models_path),
    ]
    if req.clean:
        cmd.append("--clean")
    if req.only:
        cmd.extend(["--only", req.only])
    if req.regen_dimensions:
        cmd.extend(["--regen-dimensions", *req.regen_dimensions])
    return cmd


def _run_pipeline_thread(job: dict, cfg_snapshot: dict, models_snapshot: dict, req: GenerateRequest):
    proc = None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.yaml"
            models_path = Path(tmp) / "models.yaml"
            write_yaml_secure(cfg_path, cfg_to_dict(cfg_snapshot))
            write_yaml_secure(models_path, models_snapshot)

            cmd = _build_pipeline_command(cfg_path, models_path, req)

            with _job_lock:
                if job["status"] != "running":
                    return
                job["command"] = " ".join(cmd)
            env = {**os.environ, "PYTHONUNBUFFERED": "1"}
            timed_out = threading.Event()
            stop_timers = []
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, env=env, cwd=str(REPO_ROOT),
                **process_group_popen_kwargs(),
            )
            attach_process_tree(proc)
            with _job_lock:
                job["process"] = proc
                stop_after_launch = job["status"] != "running"
            if stop_after_launch:
                stop_timer = request_process_tree_stop(
                    proc,
                    grace=_PIPELINE_KILL_GRACE,
                )
                if stop_timer is not None:
                    stop_timers.append(stop_timer)

            # Timer fires while the stdout loop is still blocking — the
            # only way to enforce a wall-clock limit on the subprocess.
            def _stop_on_timeout():
                with _job_lock:
                    should_stop = (
                        proc.poll() is None
                        and job["status"] == "running"
                    )
                    if should_stop:
                        job["status"] = "timed_out"
                        timed_out.set()
                if should_stop:
                    stop_timer = request_process_tree_stop(
                        proc,
                        grace=_PIPELINE_KILL_GRACE,
                    )
                    if stop_timer is not None:
                        stop_timers.append(stop_timer)

            timer = threading.Timer(_PIPELINE_TIMEOUT, _stop_on_timeout)
            timer.daemon = True
            timer.start()
            try:
                for line in proc.stdout:
                    clean = _ANSI_RE.sub("", line.rstrip())
                    with _job_lock:
                        _append_job_log(job, clean)
                proc.wait()
            finally:
                timer.cancel()
                with _job_lock:
                    stop_timers.extend(job.pop("_kill_timers", []))
                cancel_stop_timers(stop_timers)

            with _job_lock:
                if timed_out.is_set():
                    _append_job_log(
                        job,
                        f"ERROR: Pipeline timed out after {_PIPELINE_TIMEOUT}s",
                    )
                job["exit_code"] = proc.returncode
                # Preserve interruption states: the stopped process normally
                # exits non-zero, which must not become a generic failure.
                if job["status"] not in ("cancelled", "timed_out"):
                    job["status"] = (
                        "done" if proc.returncode == 0 else "failed"
                    )
    except Exception:
        logging.getLogger(__name__).exception("Pipeline thread failed")
        with _job_lock:
            _append_job_log(
                job,
                "ERROR: Pipeline failed unexpectedly. "
                "Check server logs for details.",
            )
            if job["status"] not in ("cancelled", "timed_out"):
                job["status"] = "failed"
            job["exit_code"] = -1
    finally:
        if proc is not None:
            release_process_tree(proc)
        with _job_lock:
            job["ended"] = time.time()
            job["elapsed"] = job["ended"] - job["started"]


def _launch_pipeline(
    job: dict,
    cfg_snapshot: dict,
    models_snapshot: dict,
    req: GenerateRequest,
) -> threading.Thread:
    """Start the background pipeline thread.

    Keeping thread creation behind this small boundary lets route tests replace
    the launcher without starting ``main.py`` or writing generation outputs.
    The thread is retained on the job so callers and tests can join it during
    controlled shutdown.
    """
    thread = threading.Thread(
        target=_run_pipeline_thread,
        args=(job, cfg_snapshot, models_snapshot, req),
        daemon=True,
        name=f"contosoforge-generation-{job['id']}",
    )
    with _job_lock:
        job["thread"] = thread
    thread.start()
    return thread


def shutdown_generation() -> None:
    """Stop and join an active pipeline during application shutdown."""
    with _job_lock:
        job = _state._current_job
        if not job or job["status"] != "running":
            return
        job["status"] = "cancelled"
        _append_job_log(
            job,
            "Pipeline cancelled because the web server is shutting down.",
        )
        proc = job.get("process")
        thread = job.get("thread")

    stop_timer = None
    if proc is not None and proc.poll() is None:
        stop_timer = request_process_tree_stop(
            proc,
            grace=_PIPELINE_KILL_GRACE,
        )
        if stop_timer is not None:
            with _job_lock:
                job.setdefault("_kill_timers", []).append(stop_timer)

    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=_PIPELINE_KILL_GRACE + 2)
        if thread.is_alive() and proc is not None and proc.poll() is None:
            force_kill_process_tree(proc)
            thread.join(timeout=5)

    if stop_timer is not None and (
        thread is None or not thread.is_alive()
    ):
        stop_timer.cancel()


@router.post("/generate")
def start_generate(req: GenerateRequest):
    # Validate request before starting thread (HTTPException can't be returned from threads)
    if req.only and req.only not in _VALID_ONLY:
        raise HTTPException(400, f"Invalid 'only' value: {req.only}. Must be one of {_VALID_ONLY}")
    if req.regen_dimensions:
        invalid = set(req.regen_dimensions) - _VALID_REGEN
        if invalid:
            raise HTTPException(400, f"Invalid regen_dimensions: {invalid}. Must be from {_VALID_REGEN}")
    with _job_lock:
        if _state._current_job and _state._current_job["status"] == "running":
            raise HTTPException(409, "A pipeline is already running.")
        job = {
            "id": str(uuid.uuid4())[:8], "status": "running",
            "logs": _new_log_buffer(), "process": None,
            "exit_code": None, "started": time.time(),
            "ended": None, "elapsed": 0, "command": "",
        }
        _state._current_job = job
    with _state._cfg_lock:
        cfg_snapshot = copy.deepcopy(_state._cfg)
        models_snapshot = copy.deepcopy(_state._models_cfg)
    _launch_pipeline(job, cfg_snapshot, models_snapshot, req)
    return {"ok": True, "job_id": job["id"]}


@router.get("/generate/stream")
async def stream_logs(after: int = Query(0, ge=0)):
    async def event_stream():
        cursor = after
        truncation_reported = False
        while True:
            with _job_lock:
                job = _state._current_job
                snapshot = None if job is None else {
                    "log_window": _log_snapshot_after(job, cursor),
                    "status": job["status"],
                    "started": job["started"],
                    "elapsed": job.get("elapsed", 0),
                    "exit_code": job.get("exit_code"),
                }
            if snapshot is None:
                yield f"data: {_json.dumps({'type': 'idle'})}\n\n"
                break
            log_window = snapshot["log_window"]
            if log_window["missing_after_cursor"] and not truncation_reported:
                truncation_reported = True
                yield (
                    "data: "
                    f"{_json.dumps({
                        'type': 'truncated',
                        'dropped': log_window['missing_after_cursor'],
                        'total_dropped': log_window['dropped'],
                        'first_sequence': log_window['first_sequence'],
                    })}\n\n"
                )
            for sequence, line in log_window["entries"]:
                yield (
                    f"id: {sequence}\n"
                    f"data: {_json.dumps({
                        'type': 'log',
                        'sequence': sequence,
                        'line': line,
                    })}\n\n"
                )
                cursor = sequence
            status = snapshot["status"]
            elapsed = (
                time.time() - snapshot["started"]
                if status == "running"
                else snapshot["elapsed"]
            )
            yield (
                "data: "
                f"{_json.dumps({
                    'type': 'status',
                    'status': status,
                    'elapsed': round(elapsed, 1),
                    'last_sequence': log_window['last_sequence'],
                })}\n\n"
            )
            if status in ("done", "failed", "cancelled", "timed_out"):
                yield (
                    "data: "
                    f"{_json.dumps({
                        'type': 'end',
                        'status': status,
                        'exit_code': snapshot['exit_code'],
                        'elapsed': round(elapsed, 1),
                        'last_sequence': log_window['last_sequence'],
                    })}\n\n"
                )
                break
            await asyncio.sleep(0.3)
    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/generate/cancel")
def cancel_generate():
    with _job_lock:
        if not _state._current_job or _state._current_job["status"] != "running":
            raise HTTPException(400, "No running job to cancel.")
        job = _state._current_job
        proc = job.get("process")
        if proc and proc.poll() is None:
            job["status"] = "cancelled"
            _append_job_log(job, "Pipeline cancelled by user.")
        else:
            raise HTTPException(400, "Could not cancel.")

    stop_timer = request_process_tree_stop(
        proc,
        grace=_PIPELINE_KILL_GRACE,
    )
    if stop_timer is not None:
        with _job_lock:
            job.setdefault("_kill_timers", []).append(stop_timer)
    return {"ok": True}


@router.get("/generate/status")
def job_status():
    with _job_lock:
        job = _state._current_job
        if not job:
            return {"status": "idle"}
        status = job["status"]
        elapsed = (
            time.time() - job["started"]
            if status == "running"
            else job.get("elapsed", 0)
        )
        log_window = _log_snapshot_after(job, 0)
        return {
            "id": job["id"], "status": status,
            "elapsed": round(elapsed, 1), "exit_code": job.get("exit_code"),
            "log_count": log_window["retained"],
            "log_dropped": log_window["dropped"],
            "first_sequence": log_window["first_sequence"],
            "last_sequence": log_window["last_sequence"],
        }
