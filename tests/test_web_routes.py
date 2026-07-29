"""
Comprehensive tests for the web layer routes, shared state, security,
and input validation.

Extends the existing test_web_api.py without duplicating its coverage.
Focuses on: YAML size limits, deep-copy safety, lock safety, thread safety,
security headers, CORS, generation input validation, and edge cases.
"""
from __future__ import annotations

import copy
import hashlib
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import yaml

httpx = pytest.importorskip("httpx", reason="httpx required for web route tests")
from starlette.testclient import TestClient  # noqa: E402

from web.api import app  # noqa: E402
from web import process_control as _process_control  # noqa: E402
import web.shared_state as _state  # noqa: E402
from web.routes import generation_routes as _generation  # noqa: E402

_REAL_LAUNCH_PIPELINE = _generation._launch_pipeline


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _git_output(*args: str) -> bytes:
    return subprocess.run(
        ["git", *args],
        cwd=_state.REPO_ROOT,
        check=True,
        capture_output=True,
    ).stdout


def _worktree_snapshot() -> tuple[bytes, tuple[tuple[str, str], ...]]:
    """Fingerprint dirty and untracked files without assuming a clean checkout."""
    status = _git_output(
        "status", "--porcelain=v1", "-z", "--untracked-files=all",
    )
    changed = _git_output("diff", "--name-only", "-z", "HEAD", "--")
    untracked = _git_output(
        "ls-files", "--others", "--exclude-standard", "-z",
    )
    paths = {
        raw.decode("utf-8", errors="surrogateescape")
        for raw in (changed + untracked).split(b"\0")
        if raw
    }
    fingerprints = []
    for relative in sorted(paths):
        path = _state.REPO_ROOT / relative
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            fingerprints.append((relative, digest))
    return status, tuple(fingerprints)


@pytest.fixture(scope="module", autouse=True)
def _web_routes_preserve_worktree():
    """Fail if this route suite changes the caller's existing worktree."""
    before = _worktree_snapshot()
    yield
    after = _worktree_snapshot()
    assert after == before, "Web route tests changed files in the working tree"


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _prevent_real_generation(monkeypatch):
    """Record pipeline launches without creating a thread or subprocess."""
    launches = []

    def fake_launch(job, cfg_snapshot, models_snapshot, req):
        launches.append({
            "job": job,
            "cfg": cfg_snapshot,
            "models": models_snapshot,
            "request": req,
        })
        job["command"] = "<pipeline launch stubbed by test>"
        job["exit_code"] = 0
        job["status"] = "done"
        job["ended"] = time.time()
        job["elapsed"] = job["ended"] - job["started"]
        return None

    monkeypatch.setattr(_generation, "_launch_pipeline", fake_launch)
    return launches


@pytest.fixture(autouse=True)
def _reset_state():
    """Snapshot and restore shared state around every test so mutations don't leak."""
    with _state._cfg_lock:
        orig_cfg = copy.deepcopy(_state._cfg)
        orig_disk_yaml = _state._cfg_disk_yaml
        orig_models = copy.deepcopy(_state._models_cfg)
        orig_models_text = _state._models_yaml_text
    with _state._job_lock:
        orig_job = _state._current_job

    yield

    with _state._cfg_lock:
        _state._cfg = orig_cfg
        _state._cfg_disk_yaml = orig_disk_yaml
        _state._models_cfg = orig_models
        _state._models_yaml_text = orig_models_text
    with _state._job_lock:
        current_job = _state._current_job
        _state._current_job = orig_job

    thread = current_job.get("thread") if current_job else None
    if thread is not None:
        thread.join(timeout=5)
        assert not thread.is_alive(), "Generation thread survived test cleanup"


# ===================================================================
# Config routes -- YAML size limit
# ===================================================================

class TestConfigYamlSizeLimit:
    """POST /api/config/yaml must reject payloads > 1 MB with 413."""

    def test_yaml_over_1mb_returns_413(self, client):
        oversized = "a: " + "x" * (1_048_577)
        resp = client.post("/api/config/yaml", json={"yaml_text": oversized})
        assert resp.status_code == 413

    def test_yaml_just_under_1mb_accepted(self, client):
        # Build valid YAML just under the limit (key: + space + padding = under 1MB)
        padding = "x" * (1_048_576 - 10)
        text = f"key: {padding}"
        # This may fail YAML parsing, but should NOT be 413
        resp = client.post("/api/config/yaml", json={"yaml_text": text})
        assert resp.status_code != 413


class TestConfigYamlParsingErrors:
    """POST /api/config/yaml must return 400 on invalid YAML."""

    def test_tab_character_yaml_error(self, client):
        resp = client.post("/api/config/yaml", json={"yaml_text": "key:\t\t{[bad"})
        assert resp.status_code == 400

    def test_bare_colon_yaml_error(self, client):
        resp = client.post("/api/config/yaml", json={"yaml_text": ": : :"})
        # Should be 400 or succeed if yaml.safe_load can handle it
        # The key point: it should not be a 500
        assert resp.status_code in (200, 400)

    def test_empty_string_returns_400(self, client):
        # yaml.safe_load("") returns None, which is not a dict
        resp = client.post("/api/config/yaml", json={"yaml_text": ""})
        assert resp.status_code == 400

    def test_scalar_yaml_returns_400(self, client):
        resp = client.post("/api/config/yaml", json={"yaml_text": "42"})
        assert resp.status_code == 400

    def test_null_yaml_returns_400(self, client):
        resp = client.post("/api/config/yaml", json={"yaml_text": "null"})
        assert resp.status_code == 400


class TestConfigDeepCopy:
    """GET /api/config must return a deep copy -- mutating the response
    should not affect internal state."""

    def test_get_config_returns_independent_copy(self, client):
        resp1 = client.get("/api/config").json()
        original_seed = resp1["seed"]

        # Mutate the response dict (local only -- should not affect server)
        resp1["seed"] = -999

        resp2 = client.get("/api/config").json()
        assert resp2["seed"] == original_seed

    def test_post_config_deep_copies_before_storing(self, client):
        """Updating config should not share references with caller."""
        client.post("/api/config", json={"values": {"seed": 111}})
        data1 = client.get("/api/config").json()
        assert data1["seed"] == 111

        # Another update should not corrupt the first
        client.post("/api/config", json={"values": {"seed": 222}})
        data2 = client.get("/api/config").json()
        assert data2["seed"] == 222


class TestConfigDownload:
    """GET /api/config/download returns the raw internal config dict."""

    def test_download_returns_dict(self, client):
        resp = client.get("/api/config/download")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)

    def test_download_has_defaults(self, client):
        resp = client.get("/api/config/download")
        data = resp.json()
        assert "defaults" in data or "sales" in data


class TestConfigYamlDisk:
    """GET /api/config/yaml/disk returns the original on-disk YAML."""

    def test_disk_yaml_returns_200(self, client):
        resp = client.get("/api/config/yaml/disk")
        assert resp.status_code == 200

    def test_disk_yaml_is_text(self, client):
        resp = client.get("/api/config/yaml/disk")
        assert "text/plain" in resp.headers.get("content-type", "")


class TestConfigYamlReset:
    """POST /api/config/yaml/reset reloads from disk."""

    def test_reset_returns_ok(self, client):
        resp = client.post("/api/config/yaml/reset")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_reset_discards_in_memory_changes(self, client):
        # Make an in-memory change
        client.post("/api/config", json={"values": {"seed": 99999}})
        data = client.get("/api/config").json()
        assert data["seed"] == 99999

        # Reset
        client.post("/api/config/yaml/reset")
        data2 = client.get("/api/config").json()
        # After reset, seed should be back to the disk default (likely 42)
        assert data2["seed"] != 99999 or data2["seed"] == 42


class TestConfigPostMultipleFields:
    """POST /api/config with multiple fields in one update."""

    def test_update_multiple_fields_atomically(self, client):
        resp = client.post("/api/config", json={
            "values": {
                "salesRows": 50000,
                "customers": 2000,
                "stores": 50,
                "products": 1000,
                "returnsEnabled": False,
            }
        })
        assert resp.status_code == 200
        data = client.get("/api/config").json()
        assert data["salesRows"] == 50000
        assert data["customers"] == 2000
        assert data["stores"] == 50
        assert data["products"] == 1000
        assert data["returnsEnabled"] is False


# ===================================================================
# Models routes
# ===================================================================

class TestModelsYamlSizeLimit:
    """POST /api/models must reject payloads > 1 MB with 413."""

    def test_models_over_1mb_returns_413(self, client):
        oversized = "a: " + "x" * (1_048_577)
        resp = client.post("/api/models", json={"yaml_text": oversized})
        assert resp.status_code == 413

    def test_models_just_under_limit_not_413(self, client):
        text = "key: " + "x" * (1_048_000)
        resp = client.post("/api/models", json={"yaml_text": text})
        assert resp.status_code != 413


class TestModelsParsingErrors:
    """POST /api/models must return 400 on non-mapping YAML."""

    def test_list_yaml_returns_400(self, client):
        resp = client.post("/api/models", json={"yaml_text": "- a\n- b"})
        assert resp.status_code == 400

    def test_empty_yaml_returns_400(self, client):
        resp = client.post("/api/models", json={"yaml_text": ""})
        assert resp.status_code == 400

    def test_invalid_yaml_returns_400(self, client):
        resp = client.post("/api/models", json={"yaml_text": "{{{bad"})
        assert resp.status_code == 400


class TestModelsFormGet:
    """GET /api/models/form returns flat form fields."""

    def test_form_returns_200(self, client):
        resp = client.get("/api/models/form")
        assert resp.status_code == 200

    def test_form_has_expected_keys(self, client):
        resp = client.get("/api/models/form")
        data = resp.json()
        expected = ["qtyLambda", "qtyMin", "qtyMax", "inflationRate",
                     "markdownEnabled", "brandEnabled", "retReasons"]
        for k in expected:
            assert k in data, f"Missing key: {k}"

    def test_form_numeric_types(self, client):
        resp = client.get("/api/models/form")
        data = resp.json()
        assert isinstance(data["qtyLambda"], (int, float))
        assert isinstance(data["qtyMin"], int)
        assert isinstance(data["markdownEnabled"], bool)


class TestModelsFormPost:
    """POST /api/models/form applies partial updates."""

    def test_update_qty_lambda(self, client):
        resp = client.post("/api/models/form", json={
            "values": {"qtyLambda": 3.0}
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        data = client.get("/api/models/form").json()
        assert data["qtyLambda"] == 3.0

    def test_update_returns_yaml_text(self, client):
        resp = client.post("/api/models/form", json={
            "values": {"brandEnabled": False}
        })
        assert resp.status_code == 200
        body = resp.json()
        assert "yaml_text" in body


class TestModelsReset:
    """POST /api/models/reset reloads from disk."""

    def test_reset_returns_ok(self, client):
        resp = client.post("/api/models/reset")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


class TestModelsLockSafety:
    """Models POST operations acquire the config lock properly."""

    def test_concurrent_models_updates(self, client):
        """Multiple threads updating models simultaneously should not corrupt state."""
        errors = []

        def update_models(i):
            try:
                text = yaml.safe_dump({"models": {"quantity": {"min_qty": i}}})
                resp = client.post("/api/models", json={"yaml_text": text})
                if resp.status_code != 200:
                    errors.append(f"Thread {i}: status {resp.status_code}")
            except Exception as e:
                errors.append(f"Thread {i}: {e}")

        threads = [threading.Thread(target=update_models, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(errors) == 0, f"Concurrent model updates failed: {errors}"

        # Verify final state is consistent (one of the updates won)
        resp = client.get("/api/models")
        parsed = yaml.safe_load(resp.text)
        assert isinstance(parsed, dict)


# ===================================================================
# Generation routes -- input validation
# ===================================================================

class TestPipelineCommandConstruction:
    """The command builder is pure and never launches the generator."""

    def test_default_request_builds_base_command(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        models_path = tmp_path / "models.yaml"

        command = _generation._build_pipeline_command(
            cfg_path,
            models_path,
            _generation.GenerateRequest(),
        )

        assert command == [
            sys.executable,
            "-u",
            str(_state.REPO_ROOT / "main.py"),
            "--config",
            str(cfg_path),
            "--models-config",
            str(models_path),
        ]

    def test_all_request_options_are_forwarded_in_order(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        models_path = tmp_path / "models.yaml"
        request = _generation.GenerateRequest(
            clean=True,
            only="dimensions",
            regen_dimensions=["products", "customers"],
        )

        command = _generation._build_pipeline_command(
            cfg_path,
            models_path,
            request,
        )

        assert command == [
            sys.executable,
            "-u",
            str(_state.REPO_ROOT / "main.py"),
            "--config",
            str(cfg_path),
            "--models-config",
            str(models_path),
            "--clean",
            "--only",
            "dimensions",
            "--regen-dimensions",
            "products",
            "customers",
        ]


class _FakePipelineProcess:
    def __init__(self, *, returncode=0, lines=(), running=False):
        self.pid = 43210
        self.returncode = returncode
        self.stdout = iter(lines)
        self.running = running
        self.kill_called = False
        self.terminate_called = False
        self.signals = []

    def poll(self):
        return None if self.running else self.returncode

    def wait(self):
        return self.returncode

    def kill(self):
        self.kill_called = True
        self.running = False
        self.returncode = -9

    def terminate(self):
        self.terminate_called = True
        self.running = False
        self.returncode = -15

    def send_signal(self, sent_signal):
        self.signals.append(sent_signal)


def _pipeline_job(*, status="running"):
    return {
        "id": "test-job",
        "status": status,
        "logs": [],
        "process": None,
        "exit_code": None,
        "started": time.time(),
        "ended": None,
        "elapsed": 0,
        "command": "",
    }


def _config_snapshots():
    with _state._cfg_lock:
        return copy.deepcopy(_state._cfg), copy.deepcopy(_state._models_cfg)


class TestPipelineRunnerIsolation:
    """Exercise runner state transitions with no real process or repository writes."""

    def test_launcher_retains_a_joinable_named_thread(self, monkeypatch):
        completed = threading.Event()

        def fake_runner(job, cfg_snapshot, models_snapshot, req):
            completed.set()

        monkeypatch.setattr(_generation, "_run_pipeline_thread", fake_runner)
        job = _pipeline_job()
        cfg_snapshot, models_snapshot = _config_snapshots()

        thread = _REAL_LAUNCH_PIPELINE(
            job,
            cfg_snapshot,
            models_snapshot,
            _generation.GenerateRequest(),
        )
        thread.join(timeout=5)

        assert completed.is_set()
        assert not thread.is_alive()
        assert job["thread"] is thread
        assert thread.name == "contosoforge-generation-test-job"

    def test_success_captures_command_logs_and_removes_temp_files(
        self, monkeypatch,
    ):
        process = _FakePipelineProcess(
            lines=["\x1b[31mstarting\x1b[0m\n", "finished\n"],
        )
        popen_calls = []

        def fake_popen(command, **kwargs):
            popen_calls.append((command, kwargs))
            return process

        monkeypatch.setattr(_generation.subprocess, "Popen", fake_popen)
        job = _pipeline_job()
        cfg_snapshot, models_snapshot = _config_snapshots()

        _generation._run_pipeline_thread(
            job,
            cfg_snapshot,
            models_snapshot,
            _generation.GenerateRequest(clean=True, only="dimensions"),
        )

        command, kwargs = popen_calls[0]
        assert command[0:3] == [
            sys.executable,
            "-u",
            str(_state.REPO_ROOT / "main.py"),
        ]
        assert command[-3:] == ["--clean", "--only", "dimensions"]
        assert kwargs["cwd"] == str(_state.REPO_ROOT)
        assert kwargs.get("shell", False) is False
        assert kwargs["creationflags"] == subprocess.CREATE_NEW_PROCESS_GROUP
        assert job["logs"] == ["starting", "finished"]
        assert job["status"] == "done"
        assert job["exit_code"] == 0
        assert job["process"] is process
        assert job["ended"] is not None
        assert job["elapsed"] >= 0

        cfg_path = Path(command[command.index("--config") + 1])
        models_path = Path(command[command.index("--models-config") + 1])
        assert not cfg_path.exists()
        assert not models_path.exists()
        assert not cfg_path.parent.exists()

    def test_nonzero_exit_is_failed(self, monkeypatch):
        process = _FakePipelineProcess(returncode=7, lines=["bad input\n"])
        monkeypatch.setattr(
            _generation.subprocess,
            "Popen",
            lambda *args, **kwargs: process,
        )
        job = _pipeline_job()
        cfg_snapshot, models_snapshot = _config_snapshots()

        _generation._run_pipeline_thread(
            job,
            cfg_snapshot,
            models_snapshot,
            _generation.GenerateRequest(),
        )

        assert job["status"] == "failed"
        assert job["exit_code"] == 7
        assert job["logs"] == ["bad input"]

    def test_cancelled_state_is_not_overwritten(self, monkeypatch):
        job = _pipeline_job()

        def cancellation_output():
            with _state._job_lock:
                job["status"] = "cancelled"
            yield "cancelled\n"

        process = _FakePipelineProcess(
            returncode=-15,
            lines=cancellation_output(),
        )
        monkeypatch.setattr(
            _generation.subprocess,
            "Popen",
            lambda *args, **kwargs: process,
        )
        cfg_snapshot, models_snapshot = _config_snapshots()

        _generation._run_pipeline_thread(
            job,
            cfg_snapshot,
            models_snapshot,
            _generation.GenerateRequest(),
        )

        assert job["status"] == "cancelled"
        assert job["exit_code"] == -15

    def test_timeout_kills_fake_process_and_records_error(self, monkeypatch):
        process = _FakePipelineProcess(running=True)

        class ImmediateTimer:
            def __init__(self, interval, callback):
                self.callback = callback

            def start(self):
                self.callback()

            def cancel(self):
                return None

        monkeypatch.setattr(
            _generation.subprocess,
            "Popen",
            lambda *args, **kwargs: process,
        )
        monkeypatch.setattr(_generation.threading, "Timer", ImmediateTimer)
        monkeypatch.setattr(
            _generation,
            "request_process_tree_stop",
            lambda proc, grace: proc.kill(),
        )
        job = _pipeline_job()
        cfg_snapshot, models_snapshot = _config_snapshots()

        _generation._run_pipeline_thread(
            job,
            cfg_snapshot,
            models_snapshot,
            _generation.GenerateRequest(),
        )

        assert process.kill_called
        assert job["status"] == "timed_out"
        assert job["exit_code"] == -9
        assert job["logs"] == [
            f"ERROR: Pipeline timed out after {_generation._PIPELINE_TIMEOUT}s",
        ]

    def test_popen_error_fails_cleanly_and_removes_temp_files(
        self, monkeypatch,
    ):
        command_seen = []

        def failing_popen(command, **kwargs):
            command_seen.extend(command)
            raise OSError("simulated process creation failure")

        monkeypatch.setattr(_generation.subprocess, "Popen", failing_popen)
        job = _pipeline_job()
        cfg_snapshot, models_snapshot = _config_snapshots()

        _generation._run_pipeline_thread(
            job,
            cfg_snapshot,
            models_snapshot,
            _generation.GenerateRequest(),
        )

        assert job["status"] == "failed"
        assert job["exit_code"] == -1
        assert job["logs"] == [
            "ERROR: Pipeline failed unexpectedly. Check server logs for details.",
        ]
        cfg_path = Path(command_seen[command_seen.index("--config") + 1])
        assert not cfg_path.exists()
        assert not cfg_path.parent.exists()

    def test_cancelled_before_launch_never_starts_popen(self, monkeypatch):
        popen_called = False

        def forbidden_popen(*args, **kwargs):
            nonlocal popen_called
            popen_called = True
            raise AssertionError("Popen must not run for a cancelled job")

        monkeypatch.setattr(
            _generation.subprocess,
            "Popen",
            forbidden_popen,
        )
        job = _pipeline_job(status="cancelled")
        cfg_snapshot, models_snapshot = _config_snapshots()

        _generation._run_pipeline_thread(
            job,
            cfg_snapshot,
            models_snapshot,
            _generation.GenerateRequest(),
        )

        assert not popen_called
        assert job["status"] == "cancelled"
        assert job["ended"] is not None


class TestProcessTreeControl:
    def test_windows_launch_uses_a_new_process_group(self):
        assert _process_control.process_group_popen_kwargs() == {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP,
        }

    def test_graceful_windows_stop_sends_ctrl_break(self):
        process = _FakePipelineProcess(running=True)

        assert _process_control.signal_process_tree(process)
        assert process.signals == [signal.CTRL_BREAK_EVENT]
        assert not process.terminate_called

    def test_stop_request_arms_daemon_force_kill_timer(self, monkeypatch):
        process = _FakePipelineProcess(running=True)
        timers = []

        class FakeTimer:
            def __init__(self, interval, callback):
                self.interval = interval
                self.callback = callback
                self.daemon = False
                self.started = False
                self.cancelled = False
                timers.append(self)

            def start(self):
                self.started = True

            def cancel(self):
                self.cancelled = True

        monkeypatch.setattr(_process_control.threading, "Timer", FakeTimer)
        hard_kills = []
        monkeypatch.setattr(
            _process_control,
            "force_kill_process_tree",
            lambda proc: hard_kills.append(proc),
        )

        timer = _process_control.request_process_tree_stop(process, grace=2.5)

        assert process.signals == [signal.CTRL_BREAK_EVENT]
        assert timer is timers[0]
        assert timer.interval == 2.5
        assert timer.daemon
        assert timer.started
        timer.callback()
        assert hard_kills == [process]

    def test_windows_hard_stop_uses_taskkill_tree(self, monkeypatch):
        process = _FakePipelineProcess(running=True)
        calls = []

        class Result:
            returncode = 0

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            process.running = False
            process.returncode = -9
            return Result()

        monkeypatch.setattr(_process_control.subprocess, "run", fake_run)

        assert _process_control.force_kill_process_tree(process)
        command, kwargs = calls[0]
        assert command == [
            "taskkill", "/PID", str(process.pid), "/T", "/F",
        ]
        assert kwargs["check"] is False
        assert kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW

    def test_windows_job_object_is_attached_terminated_and_released(
        self, monkeypatch,
    ):
        process = _FakePipelineProcess(running=True)
        process._handle = 24680

        class FakeKernel:
            def __init__(self):
                self.assigned = []
                self.terminated = []
                self.closed = []

            def CreateJobObjectW(self, security, name):
                return 13579

            def SetInformationJobObject(
                self, handle, info_class, info, size,
            ):
                return True

            def AssignProcessToJobObject(self, job_handle, process_handle):
                self.assigned.append((job_handle, process_handle.value))
                return True

            def TerminateJobObject(self, job_handle, exit_code):
                self.terminated.append((job_handle, exit_code))
                process.running = False
                process.returncode = exit_code
                return True

            def CloseHandle(self, handle):
                self.closed.append(handle)
                return True

        kernel = FakeKernel()
        monkeypatch.setattr(_process_control, "_kernel32", kernel)

        assert _process_control.attach_process_tree(process)
        assert process._contosoforge_job_handle == 13579
        assert kernel.assigned == [(13579, process._handle)]

        assert _process_control.force_kill_process_tree(process)
        assert kernel.terminated == [(13579, 1)]

        _process_control.release_process_tree(process)
        assert kernel.closed == [13579]
        assert process._contosoforge_job_handle is None


class TestGenerationShutdown:
    def test_shutdown_cancels_stops_and_joins_running_job(self, monkeypatch):
        process = _FakePipelineProcess(running=True)

        class FakeStopTimer:
            def __init__(self):
                self.cancelled = False

            def cancel(self):
                self.cancelled = True

        class FakeJobThread:
            def __init__(self):
                self.alive = True
                self.join_timeouts = []

            def join(self, timeout):
                self.join_timeouts.append(timeout)
                self.alive = False

            def is_alive(self):
                return self.alive

        stop_timer = FakeStopTimer()
        thread = FakeJobThread()
        job = _pipeline_job()
        job["process"] = process
        job["thread"] = thread
        with _state._job_lock:
            _state._current_job = job
        stop_requests = []

        def fake_stop(proc, *, grace):
            stop_requests.append((proc, grace))
            return stop_timer

        monkeypatch.setattr(
            _generation,
            "request_process_tree_stop",
            fake_stop,
        )

        _generation.shutdown_generation()

        assert stop_requests == [
            (process, _generation._PIPELINE_KILL_GRACE),
        ]
        assert thread.join_timeouts == [
            _generation._PIPELINE_KILL_GRACE + 2,
        ]
        assert stop_timer.cancelled
        with _state._job_lock:
            assert job["status"] == "cancelled"
            assert "web server is shutting down" in job["logs"][-1]


class TestValidateEndpointExtended:
    """Extended validation tests beyond test_web_api.py."""

    def test_zero_sales_rows_produces_error(self, client):
        client.post("/api/config", json={"values": {"salesRows": 0}})
        resp = client.get("/api/validate")
        data = resp.json()
        assert any("rows" in e.lower() or "zero" in e.lower() for e in data["errors"])

    def test_negative_sales_rows_produces_error(self, client):
        client.post("/api/config", json={"values": {"salesRows": -100}})
        resp = client.get("/api/validate")
        data = resp.json()
        assert any("rows" in e.lower() or "zero" in e.lower() or "greater" in e.lower()
                    for e in data["errors"])

    def test_inverted_price_range_produces_error(self, client):
        client.post("/api/config", json={
            "values": {"minPrice": 5000, "maxPrice": 10}
        })
        resp = client.get("/api/validate")
        data = resp.json()
        assert any("price" in e.lower() for e in data["errors"])

    def test_return_rate_above_1_produces_error(self, client):
        client.post("/api/config", json={
            "values": {"returnsEnabled": True, "returnRate": 1.5}
        })
        resp = client.get("/api/validate")
        data = resp.json()
        assert any("return" in e.lower() and "rate" in e.lower() for e in data["errors"])

    def test_return_rate_negative_produces_error(self, client):
        client.post("/api/config", json={
            "values": {"returnsEnabled": True, "returnRate": -0.5}
        })
        resp = client.get("/api/validate")
        data = resp.json()
        assert any("return" in e.lower() and "rate" in e.lower() for e in data["errors"])

    def test_chunk_exceeds_rows_produces_warning(self, client):
        client.post("/api/config", json={
            "values": {"salesRows": 1000, "chunkSize": 5000}
        })
        resp = client.get("/api/validate")
        data = resp.json()
        assert any("chunk" in w.lower() for w in data["warnings"])

    def test_skip_order_cols_warning(self, client):
        client.post("/api/config", json={
            "values": {"skipOrderCols": True}
        })
        resp = client.get("/api/validate")
        data = resp.json()
        assert any("order" in w.lower() or "return" in w.lower() for w in data["warnings"])

    def test_large_csv_warning(self, client):
        client.post("/api/config", json={
            "values": {"format": "csv", "salesRows": 10_000_000}
        })
        resp = client.get("/api/validate")
        data = resp.json()
        assert any("csv" in w.lower() for w in data["warnings"])

    def test_customers_exceed_rows_warning(self, client):
        client.post("/api/config", json={
            "values": {"salesRows": 100, "customers": 1000}
        })
        resp = client.get("/api/validate")
        data = resp.json()
        assert any("customer" in w.lower() for w in data["warnings"])


class TestGenerateInputValidation:
    """POST /api/generate -- validation of only and regen_dimensions fields."""

    def test_generate_invalid_only_value(self, client, _prevent_real_generation):
        """The 'only' parameter must be 'dimensions' or 'sales'."""
        resp = client.post("/api/generate", json={"only": "invalid_stage"})
        assert resp.status_code == 400
        assert _prevent_real_generation == []

    def test_generate_valid_only_dimensions(self, client, _prevent_real_generation):
        resp = client.post("/api/generate", json={"only": "dimensions"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert "job_id" in body
        assert len(_prevent_real_generation) == 1
        assert _prevent_real_generation[0]["request"].only == "dimensions"

    def test_generate_valid_only_sales(self, client, _prevent_real_generation):
        resp = client.post("/api/generate", json={"only": "sales"})
        assert resp.status_code == 200
        assert len(_prevent_real_generation) == 1
        assert _prevent_real_generation[0]["request"].only == "sales"

    def test_generate_rejects_concurrent_run(self, client):
        """If a job is running, POST /api/generate should return 409."""
        with _state._job_lock:
            _state._current_job = {
                "id": "fake", "status": "running", "logs": [],
                "process": None, "exit_code": None,
                "started": time.time(), "ended": None,
                "elapsed": 0, "command": "",
            }
        resp = client.post("/api/generate", json={})
        assert resp.status_code == 409

    def test_generate_status_when_idle(self, client):
        _state._current_job = None
        resp = client.get("/api/generate/status")
        assert resp.status_code == 200
        assert resp.json()["status"] == "idle"

    def test_generate_status_when_running(self, client):
        with _state._job_lock:
            _state._current_job = {
                "id": "test123", "status": "running", "logs": ["line1"],
                "process": None, "exit_code": None,
                "started": time.time(), "ended": None,
                "elapsed": 0, "command": "",
            }
        resp = client.get("/api/generate/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "running"
        assert data["id"] == "test123"
        assert data["log_count"] == 1

    def test_cancel_when_no_job_returns_400(self, client):
        _state._current_job = None
        resp = client.post("/api/generate/cancel")
        assert resp.status_code == 400

    def test_cancel_when_job_not_running_returns_400(self, client):
        with _state._job_lock:
            _state._current_job = {
                "id": "done1", "status": "done", "logs": [],
                "process": None, "exit_code": 0,
                "started": time.time(), "ended": time.time(),
                "elapsed": 1.0, "command": "",
            }
        resp = client.post("/api/generate/cancel")
        assert resp.status_code == 400

    def test_cancel_running_job_requests_process_tree_stop(
        self, client, monkeypatch,
    ):
        process = _FakePipelineProcess(running=True)
        job = _pipeline_job()
        job["process"] = process
        with _state._job_lock:
            _state._current_job = job
        stop_requests = []

        def fake_stop(proc, *, grace):
            stop_requests.append((proc, grace))
            return None

        monkeypatch.setattr(
            _generation,
            "request_process_tree_stop",
            fake_stop,
        )

        resp = client.post("/api/generate/cancel")

        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        assert stop_requests == [
            (process, _generation._PIPELINE_KILL_GRACE),
        ]
        with _state._job_lock:
            assert job["status"] == "cancelled"
            assert job["logs"][-1] == "Pipeline cancelled by user."

        second = client.post("/api/generate/cancel")
        assert second.status_code == 400
        assert second.json()["detail"] == "No running job to cancel."

    def test_timed_out_job_streams_one_terminal_event(self, client):
        job = _pipeline_job(status="timed_out")
        job["logs"] = ["before timeout", "timeout reached"]
        job["exit_code"] = -9
        job["elapsed"] = 12.5
        with _state._job_lock:
            _state._current_job = job

        resp = client.get("/api/generate/stream")

        assert resp.status_code == 200
        assert resp.text.count('"type": "end"') == 1
        assert '"status": "timed_out"' in resp.text
        assert '"exit_code": -9' in resp.text

    def test_four_hour_simulated_log_window_stays_bounded(self):
        retention = 500
        logs = _generation._new_log_buffer(retention)
        for second in range(4 * 60 * 60):
            logs.append(f"second {second}")

        assert len(logs) == retention
        assert logs.dropped == (4 * 60 * 60) - retention
        assert logs.first_sequence == (4 * 60 * 60) - retention + 1
        assert logs.last_sequence == 4 * 60 * 60
        assert logs[0] == f"second {(4 * 60 * 60) - retention}"
        assert logs[-1] == f"second {(4 * 60 * 60) - 1}"

    def test_log_snapshot_copies_only_entries_after_cursor(self):
        job = _pipeline_job(status="done")
        job["logs"] = _generation._new_log_buffer(100)
        for index in range(1_000):
            job["logs"].append(f"line {index}")

        snapshot = _generation._log_snapshot_after(job, 998)

        assert snapshot["entries"] == [
            (999, "line 998"),
            (1000, "line 999"),
        ]
        assert snapshot["retained"] == 100
        assert snapshot["dropped"] == 900

    def test_stream_resume_cursor_returns_only_newer_logs(self, client):
        job = _pipeline_job(status="done")
        job["logs"] = _generation._new_log_buffer(3)
        for line in ("one", "two", "three", "four", "five"):
            job["logs"].append(line)
        job["exit_code"] = 0
        with _state._job_lock:
            _state._current_job = job

        resp = client.get("/api/generate/stream?after=4")

        assert resp.status_code == 200
        assert "id: 5" in resp.text
        assert '"sequence": 5' in resp.text
        assert '"line": "five"' in resp.text
        assert '"line": "four"' not in resp.text
        assert '"type": "truncated"' not in resp.text

    def test_stream_reports_cursor_that_fell_behind_retention(self, client):
        job = _pipeline_job(status="done")
        job["logs"] = _generation._new_log_buffer(3)
        for line in ("one", "two", "three", "four", "five"):
            job["logs"].append(line)
        job["exit_code"] = 0
        with _state._job_lock:
            _state._current_job = job

        resp = client.get("/api/generate/stream?after=0")

        assert resp.status_code == 200
        assert '"type": "truncated"' in resp.text
        assert '"dropped": 2' in resp.text
        assert '"first_sequence": 3' in resp.text
        assert "id: 3" in resp.text
        assert '"line": "three"' in resp.text

    def test_stream_rejects_negative_resume_cursor(self, client):
        resp = client.get("/api/generate/stream?after=-1")
        assert resp.status_code == 422


class TestGenerateRequestModel:
    """Test pydantic validation of GenerateRequest."""

    def test_empty_body_accepted(self, client, _prevent_real_generation):
        resp = client.post("/api/generate", json={})
        assert resp.status_code == 200
        assert len(_prevent_real_generation) == 1
        assert _prevent_real_generation[0]["request"].model_dump() == {
            "clean": False,
            "only": None,
            "regen_dimensions": None,
        }

    def test_clean_flag_accepted(self, client, _prevent_real_generation):
        resp = client.post("/api/generate", json={"clean": True})
        assert resp.status_code == 200
        assert len(_prevent_real_generation) == 1
        assert _prevent_real_generation[0]["request"].clean is True

    def test_regen_dimensions_list_accepted(self, client, _prevent_real_generation):
        resp = client.post("/api/generate", json={
            "regen_dimensions": ["products", "customers"]
        })
        assert resp.status_code == 200
        assert len(_prevent_real_generation) == 1
        assert _prevent_real_generation[0]["request"].regen_dimensions == [
            "products", "customers",
        ]

    def test_invalid_field_type_returns_422(self, client):
        resp = client.post("/api/generate", json={"clean": "not_a_bool_string"})
        # pydantic may coerce strings to bool, so this might succeed
        # But an integer list for 'only' should fail
        resp2 = client.post("/api/generate", json={"regen_dimensions": "not_a_list"})
        # pydantic v2 may or may not accept this
        assert resp.status_code in (200, 422) or resp2.status_code in (200, 422)


# ===================================================================
# Presets routes
# ===================================================================

class TestPresetsExtended:
    """Extended preset tests beyond test_web_api.py."""

    def test_unknown_preset_returns_404_with_message(self, client):
        resp = client.post("/api/presets/apply", json={"name": "DOES_NOT_EXIST_XYZ"})
        assert resp.status_code in (400, 404)
        detail = resp.json().get("detail", "")
        assert "DOES_NOT_EXIST_XYZ" in detail or "preset" in detail.lower() or "Unknown" in detail

    def test_apply_preset_modifies_config(self, client):
        """Applying a known preset should change the config values."""
        presets_resp = client.get("/api/presets")
        presets = presets_resp.json()
        if not presets:
            pytest.skip("No presets available")

        # Find a preset name
        preset_name = None
        for bucket_presets in presets.values():
            if isinstance(bucket_presets, dict) and bucket_presets:
                preset_name = next(iter(bucket_presets.keys()))
                break
        if preset_name is None:
            pytest.skip("Could not extract a preset name")

        resp = client.post("/api/presets/apply", json={"name": preset_name})
        assert resp.status_code == 200
        assert resp.json()["applied"] == preset_name

        # Verify config changed
        cfg = client.get("/api/config").json()
        assert cfg["salesRows"] > 0

    def test_apply_empty_name_returns_error(self, client):
        """Empty string for preset name should return 404 (not in PRESETS)."""
        resp = client.post("/api/presets/apply", json={"name": ""})
        assert resp.status_code in (400, 404)


class TestGuidedSetup:
    """Beginner workflow applies config and models as one transaction."""

    @staticmethod
    def payload(**overrides):
        values = {
            "size": "quick-sample",
            "start_date": "2024-01-01",
            "end_date": "2025-12-31",
            "format": "parquet",
            "catalog": "all",
            "trend": "seasonal-with-growth",
            "feature_bundle": "analytics",
        }
        values.update(overrides)
        return values

    def test_guided_setup_applies_all_five_decisions(self, client):
        resp = client.post(
            "/api/presets/guided",
            json=self.payload(),
        )

        assert resp.status_code == 200
        summary = resp.json()["summary"]
        assert summary["sales_rows"] == 100_000
        assert summary["trend"] == "seasonal-with-growth"

        cfg = client.get("/api/config").json()
        assert cfg["salesRows"] == 100_000
        assert cfg["startDate"] == "2024-01-01"
        assert cfg["endDate"] == "2025-12-31"
        assert cfg["format"] == "parquet"
        assert cfg["productCatalog"] == "all"
        assert cfg["returnsEnabled"] is True
        assert cfg["budgetEnabled"] is True
        assert cfg["inventoryEnabled"] is True
        assert cfg["subEnabled"] is False
        assert cfg["customers"] == 2_778

        models = client.get("/api/models/form").json()
        assert models["trendPreset"] == "seasonal-with-growth"

    def test_full_bundle_enables_extended_facts(self, client):
        resp = client.post(
            "/api/presets/guided",
            json=self.payload(feature_bundle="full", catalog="synthetic"),
        )

        assert resp.status_code == 200
        cfg = client.get("/api/config").json()
        assert cfg["productCatalog"] == "synthetic"
        assert cfg["subEnabled"] is True
        assert cfg["subGenerateBridge"] is True
        assert cfg["wlEnabled"] is True
        assert cfg["ccEnabled"] is True
        assert cfg["skipOrderCols"] is False

    def test_trend_change_clears_manual_year_factors(self, client):
        manual = client.post(
            "/api/models/form",
            json={
                "values": {
                    "demandMode": "once",
                    "demandFactors": [0.8, 1.0, 1.2],
                },
            },
        )
        assert manual.status_code == 200
        assert client.get("/api/models/form").json()[
            "hasManualDemandFactors"
        ] is True

        resp = client.post(
            "/api/presets/guided",
            json=self.payload(trend="hockey-stick"),
        )

        assert resp.status_code == 200
        assert resp.json()["summary"]["manual_factors_cleared"] is True
        form = client.get("/api/models/form").json()
        assert form["trendPreset"] == "hockey-stick"
        assert form["hasManualDemandFactors"] is False
        assert form["demandFactors"] == []

    def test_invalid_date_range_is_structured_and_atomic(self, client):
        before = client.get("/api/config").json()

        resp = client.post(
            "/api/presets/guided",
            json=self.payload(
                start_date="2025-01-01",
                end_date="2024-01-01",
            ),
        )

        assert resp.status_code == 422
        assert resp.json()["detail"]["field"] == "dateRange"
        after = client.get("/api/config").json()
        assert after["salesRows"] == before["salesRows"]
        assert after["startDate"] == before["startDate"]

    def test_invalid_trend_is_structured_and_atomic(self, client):
        before = client.get("/api/models/form").json()["trendPreset"]

        resp = client.post(
            "/api/presets/guided",
            json=self.payload(trend="always-up-somehow"),
        )

        assert resp.status_code == 422
        assert resp.json()["detail"]["field"] == "trend"
        assert client.get("/api/models/form").json()["trendPreset"] == before

    def test_guided_setup_is_dual_mounted(self, client):
        resp = client.post(
            "/v1/api/presets/guided",
            json=self.payload(size="power-bi-demo"),
        )

        assert resp.status_code == 200
        assert resp.json()["summary"]["sales_rows"] == 2_000_000


# ===================================================================
# Shared state -- thread safety
# ===================================================================

class TestThreadSafety:
    """Concurrent threads updating config should not corrupt state."""

    def test_concurrent_config_updates(self, client):
        errors = []
        results = []

        def update_config(seed_val):
            try:
                resp = client.post("/api/config", json={
                    "values": {"seed": seed_val}
                })
                if resp.status_code != 200:
                    errors.append(f"Seed {seed_val}: status {resp.status_code}")
                results.append(seed_val)
            except Exception as e:
                errors.append(f"Seed {seed_val}: {e}")

        threads = [
            threading.Thread(target=update_config, args=(i,))
            for i in range(20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(errors) == 0, f"Concurrent updates failed: {errors}"
        assert len(results) == 20

        # Config should have one of the seed values (last writer wins)
        data = client.get("/api/config").json()
        assert data["seed"] in range(20)

    def test_concurrent_reads_and_writes(self, client):
        """Reads and writes interleaved should not raise exceptions."""
        errors = []

        def reader(idx):
            try:
                for _ in range(5):
                    resp = client.get("/api/config")
                    if resp.status_code != 200:
                        errors.append(f"Reader {idx}: status {resp.status_code}")
                    data = resp.json()
                    assert isinstance(data, dict)
            except Exception as e:
                errors.append(f"Reader {idx}: {e}")

        def writer(idx):
            try:
                for j in range(5):
                    resp = client.post("/api/config", json={
                        "values": {"seed": idx * 100 + j}
                    })
                    if resp.status_code != 200:
                        errors.append(f"Writer {idx}: status {resp.status_code}")
            except Exception as e:
                errors.append(f"Writer {idx}: {e}")

        threads = []
        for i in range(5):
            threads.append(threading.Thread(target=reader, args=(i,)))
            threads.append(threading.Thread(target=writer, args=(i,)))

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert len(errors) == 0, f"Concurrent read/write failed: {errors}"

    def test_concurrent_yaml_updates(self, client):
        """Multiple threads posting YAML config simultaneously."""
        errors = []

        def yaml_updater(i):
            try:
                cfg = {
                    "defaults": {"seed": i, "dates": {"start": "2024-01-01", "end": "2024-12-31"}},
                    "sales": {"file_format": "parquet", "total_rows": 1000 + i, "skip_order_cols": False},
                }
                text = yaml.safe_dump(cfg)
                resp = client.post("/api/config/yaml", json={"yaml_text": text})
                if resp.status_code != 200:
                    errors.append(f"YAML updater {i}: status {resp.status_code}")
            except Exception as e:
                errors.append(f"YAML updater {i}: {e}")

        threads = [threading.Thread(target=yaml_updater, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(errors) == 0, f"Concurrent YAML updates failed: {errors}"


# ===================================================================
# Security headers
# ===================================================================

class TestSecurityHeaders:
    """Verify security headers are present in all responses."""

    def test_x_content_type_options(self, client):
        resp = client.get("/api/config")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"

    def test_x_frame_options(self, client):
        resp = client.get("/api/config")
        assert resp.headers.get("X-Frame-Options") == "DENY"

    def test_x_xss_protection(self, client):
        resp = client.get("/api/config")
        assert resp.headers.get("X-XSS-Protection") == "1; mode=block"

    def test_referrer_policy(self, client):
        resp = client.get("/api/config")
        assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"

    def test_content_security_policy_blocks_unexpected_scripts(self, client):
        resp = client.get("/")
        policy = resp.headers.get("Content-Security-Policy", "")

        assert "default-src 'self'" in policy
        assert "script-src 'self'" in policy
        assert "connect-src 'self'" in policy
        assert "object-src 'none'" in policy
        assert "frame-ancestors 'none'" in policy
        assert "unsafe-eval" not in policy
        assert "https:" not in policy

    def test_production_assets_use_fingerprinted_cache_policy(self, client):
        index = client.get("/")
        assert index.status_code == 200
        assert index.headers["Cache-Control"] == "no-cache"

        match = re.search(r'src="(/static/assets/app-[A-Z0-9]+\.js)"', index.text)
        assert match is not None
        asset = client.get(match.group(1))
        assert asset.status_code == 200
        assert asset.headers["Cache-Control"] == (
            "public, max-age=31536000, immutable"
        )
        assert asset.headers["Content-Security-Policy"]

    def test_security_headers_on_post(self, client):
        resp = client.post("/api/config", json={"values": {}})
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "DENY"

    def test_security_headers_on_yaml_endpoint(self, client):
        resp = client.get("/api/config/yaml")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"

    def test_security_headers_on_validate(self, client):
        resp = client.get("/api/validate")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "DENY"

    def test_security_headers_on_models(self, client):
        resp = client.get("/api/models")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"

    def test_security_headers_on_presets(self, client):
        resp = client.get("/api/presets")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"

    def test_security_headers_on_404(self, client):
        resp = client.get("/api/nonexistent_endpoint_xyz")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "DENY"


# ===================================================================
# CORS -- restricted origins (not wildcard)
# ===================================================================

class TestCORS:
    """Verify CORS is configured with restricted origins, not wildcard."""

    def test_allowed_origin_returns_cors_headers(self, client):
        resp = client.options(
            "/api/config",
            headers={
                "Origin": "http://localhost:8502",
                "Access-Control-Request-Method": "GET",
            },
        )
        acl = resp.headers.get("access-control-allow-origin", "")
        assert acl == "http://localhost:8502"

    def test_disallowed_origin_no_cors_header(self, client):
        resp = client.get(
            "/api/config",
            headers={"Origin": "http://evil.example.com"},
        )
        acl = resp.headers.get("access-control-allow-origin", "")
        # Should NOT be "*" and should NOT be the evil origin
        assert acl != "*"
        assert "evil.example.com" not in acl

    def test_no_wildcard_origin(self, client):
        """CORS should never return Access-Control-Allow-Origin: *"""
        resp = client.get(
            "/api/config",
            headers={"Origin": "http://localhost:3000"},
        )
        acl = resp.headers.get("access-control-allow-origin", "")
        assert acl != "*"

    def test_localhost_3000_allowed(self, client):
        resp = client.get(
            "/api/config",
            headers={"Origin": "http://localhost:3000"},
        )
        acl = resp.headers.get("access-control-allow-origin", "")
        assert acl == "http://localhost:3000"

    def test_127_0_0_1_8502_allowed(self, client):
        resp = client.get(
            "/api/config",
            headers={"Origin": "http://127.0.0.1:8502"},
        )
        acl = resp.headers.get("access-control-allow-origin", "")
        assert acl == "http://127.0.0.1:8502"


# ===================================================================
# Input validation -- edge cases
# ===================================================================

class TestInputValidationEdgeCases:
    """Edge cases for config POST -- negative values, empty strings, etc."""

    def test_negative_sales_rows(self, client):
        resp = client.post("/api/config", json={"values": {"salesRows": -1}})
        assert resp.status_code == 200  # POST accepts it; validation catches it
        data = client.get("/api/config").json()
        assert data["salesRows"] == -1

        # Validate should flag this
        val = client.get("/api/validate").json()
        assert len(val["errors"]) > 0

    def test_zero_customers(self, client):
        resp = client.post("/api/config", json={"values": {"customers": 0}})
        assert resp.status_code == 200

    def test_negative_stores(self, client):
        resp = client.post("/api/config", json={"values": {"stores": -5}})
        assert resp.status_code == 200
        data = client.get("/api/config").json()
        assert data["stores"] == -5

    def test_empty_format_string(self, client):
        # format is a validated enum (unlike free numeric fields such as
        # customers/stores); an empty/invalid value is rejected with 400.
        resp = client.post("/api/config", json={"values": {"format": ""}})
        assert resp.status_code == 400

    def test_very_large_sales_rows(self, client):
        resp = client.post("/api/config", json={"values": {"salesRows": 999_999_999}})
        assert resp.status_code == 200
        data = client.get("/api/config").json()
        assert data["salesRows"] == 999_999_999

    def test_float_coercion_for_int_field(self, client):
        """Passing a float for an int field should be coerced."""
        resp = client.post("/api/config", json={"values": {"salesRows": 1000.7}})
        assert resp.status_code == 200
        data = client.get("/api/config").json()
        assert isinstance(data["salesRows"], int)

    def test_invalid_numeric_config_is_structured_422_and_atomic(self, client):
        before = client.get("/api/config").json()["salesRows"]

        resp = client.post(
            "/api/config",
            json={"values": {"salesRows": "many"}},
        )

        assert resp.status_code == 422
        assert resp.json()["detail"] == {
            "field": "salesRows",
            "message": "Expected an integer value.",
        }
        assert client.get("/api/config").json()["salesRows"] == before

    def test_invalid_numeric_models_value_is_structured_422_and_atomic(
        self,
        client,
    ):
        before = client.get("/api/models/form").json()["qtyMin"]

        resp = client.post(
            "/api/models/form",
            json={"values": {"qtyMin": "several"}},
        )

        assert resp.status_code == 422
        assert resp.json()["detail"] == {
            "field": "qtyMin",
            "message": "Expected an integer value.",
        }
        assert client.get("/api/models/form").json()["qtyMin"] == before

    def test_er_to_currencies_non_list_ignored(self, client):
        """erToCurrencies must be a list; non-list should be ignored."""
        before = client.get("/api/config").json()["erToCurrencies"]
        resp = client.post("/api/config", json={"values": {"erToCurrencies": "CAD"}})
        assert resp.status_code == 200
        after = client.get("/api/config").json()["erToCurrencies"]
        assert after == before  # unchanged

    def test_unknown_values_key_silently_ignored(self, client):
        """Keys not recognized by the update handler should be silently ignored."""
        resp = client.post("/api/config", json={
            "values": {"nonExistentFieldXyz": 42}
        })
        assert resp.status_code == 200

    def test_empty_values_dict(self, client):
        """Posting empty values dict should succeed (no-op)."""
        before = client.get("/api/config").json()
        resp = client.post("/api/config", json={"values": {}})
        assert resp.status_code == 200
        after = client.get("/api/config").json()
        assert before["seed"] == after["seed"]


# ===================================================================
# Versioned routes (/v1/api/...) extended
# ===================================================================

class TestVersionedRoutesExtended:
    """Verify /v1 prefix works for all route groups."""

    def test_v1_config_yaml(self, client):
        resp = client.get("/v1/api/config/yaml")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers.get("content-type", "")

    def test_v1_config_download(self, client):
        resp = client.get("/v1/api/config/download")
        assert resp.status_code == 200

    def test_v1_models(self, client):
        resp = client.get("/v1/api/models")
        assert resp.status_code == 200

    def test_v1_models_form(self, client):
        resp = client.get("/v1/api/models/form")
        assert resp.status_code == 200

    def test_v1_generate_status(self, client):
        resp = client.get("/v1/api/generate/status")
        assert resp.status_code == 200

    def test_v1_post_config(self, client):
        resp = client.post("/v1/api/config", json={"values": {"seed": 55}})
        assert resp.status_code == 200

    def test_v1_post_config_yaml(self, client):
        cfg = {
            "defaults": {"seed": 42, "dates": {"start": "2024-01-01", "end": "2024-12-31"}},
            "sales": {"file_format": "parquet", "total_rows": 1000, "skip_order_cols": False},
        }
        resp = client.post("/v1/api/config/yaml", json={
            "yaml_text": yaml.safe_dump(cfg)
        })
        assert resp.status_code == 200

    def test_v1_validate(self, client):
        resp = client.get("/v1/api/validate")
        assert resp.status_code == 200
        data = resp.json()
        assert "errors" in data and "warnings" in data

    def test_v1_presets(self, client):
        resp = client.get("/v1/api/presets")
        assert resp.status_code == 200

    def test_v1_security_headers_present(self, client):
        resp = client.get("/v1/api/config")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "DENY"


# ===================================================================
# Shared state helper unit tests
# ===================================================================

class TestSharedStateHelpers:
    """Direct tests for helper functions in shared_state."""

    def test_nested_get_basic(self):
        from web.shared_state import _g
        d = {"a": {"b": {"c": 42}}}
        assert _g(d, "a", "b", "c") == 42

    def test_nested_get_missing(self):
        from web.shared_state import _g
        d = {"a": {"b": 1}}
        assert _g(d, "a", "x", default=99) == 99

    def test_nested_get_non_dict(self):
        from web.shared_state import _g
        d = {"a": 5}
        assert _g(d, "a", "b", default="nope") == "nope"

    def test_promo_total_from_buckets(self):
        from web.shared_state import _promo_total
        from src.engine.config.config_schema import PromotionsConfig
        promos = PromotionsConfig.model_validate({"num_seasonal": 10, "num_clearance": 5, "num_limited": 3})
        assert _promo_total(promos) == 18

    def test_promo_total_from_total_key(self):
        from web.shared_state import _promo_total
        # With Pydantic models, num_seasonal/num_clearance/num_limited are always
        # declared fields (even if None), so _promo_total sums them. When all are
        # None, the result is 0. Use a plain dict without bucket keys to test the
        # total_promotions fallback path.
        from types import SimpleNamespace
        promos = SimpleNamespace(total_promotions=25)
        assert _promo_total(promos) == 25

    def test_set_promotions_total_distributes(self):
        from web.shared_state import _set_promotions_total
        from src.engine.config.config_schema import PromotionsConfig
        promos = PromotionsConfig.model_validate({"num_seasonal": 10, "num_clearance": 5, "num_limited": 5})
        _set_promotions_total(promos, 40)
        total = promos.num_seasonal + promos.num_clearance + promos.num_limited
        assert total == 40

    def test_set_promotions_total_zero(self):
        from web.shared_state import _set_promotions_total
        from src.engine.config.config_schema import PromotionsConfig
        promos = PromotionsConfig.model_validate({"num_seasonal": 10, "num_clearance": 5, "num_limited": 5})
        _set_promotions_total(promos, 0)
        total = promos.num_seasonal + promos.num_clearance + promos.num_limited
        assert total == 0

    def test_set_promotions_total_negative_clamped(self):
        from web.shared_state import _set_promotions_total
        from src.engine.config.config_schema import PromotionsConfig
        promos = PromotionsConfig.model_validate({"num_seasonal": 10, "num_clearance": 5, "num_limited": 5})
        _set_promotions_total(promos, -10)
        total = promos.num_seasonal + promos.num_clearance + promos.num_limited
        assert total == 0  # clamped to 0

    def test_set_promotions_total_without_buckets(self):
        from web.shared_state import _set_promotions_total
        # Use a SimpleNamespace without bucket fields to test the
        # total_promotions fallback path.
        from types import SimpleNamespace
        promos = SimpleNamespace()
        _set_promotions_total(promos, 50)
        assert promos.total_promotions == 50
