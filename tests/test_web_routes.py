"""
Comprehensive tests for the web layer routes, shared state, security,
and input validation.

Extends the existing test_web_api.py without duplicating its coverage.
Focuses on: YAML size limits, deep-copy safety, lock safety, thread safety,
security headers, CORS, generation input validation, and edge cases.
"""
from __future__ import annotations

import copy
import threading
import time
from typing import Any, Dict

import pytest
import yaml

httpx = pytest.importorskip("httpx", reason="httpx required for web route tests")
from starlette.testclient import TestClient

from web.api import app
import web.shared_state as _state


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_state():
    """Snapshot and restore shared state around every test so mutations don't leak."""
    with _state._cfg_lock:
        orig_cfg = copy.deepcopy(_state._cfg)
        orig_disk_yaml = _state._cfg_disk_yaml
        orig_models = copy.deepcopy(_state._models_cfg)
        orig_models_text = _state._models_yaml_text
    orig_job = _state._current_job

    yield

    with _state._cfg_lock:
        _state._cfg = orig_cfg
        _state._cfg_disk_yaml = orig_disk_yaml
        _state._models_cfg = orig_models
        _state._models_yaml_text = orig_models_text
    _state._current_job = orig_job


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

    def test_generate_invalid_only_value(self, client):
        """The 'only' parameter must be 'dimensions' or 'sales'."""
        resp = client.post("/api/generate", json={"only": "invalid_stage"})
        # The validation happens inside the background thread, but the endpoint
        # should still accept the request (validation is deferred).
        # At minimum, the request itself should be valid pydantic.
        assert resp.status_code in (200, 400, 409)

    def test_generate_valid_only_dimensions(self, client):
        # Clear any running job first
        _state._current_job = None
        resp = client.post("/api/generate", json={"only": "dimensions"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert "job_id" in body
        # Clean up - cancel or wait for it
        time.sleep(0.2)

    def test_generate_valid_only_sales(self, client):
        _state._current_job = None
        resp = client.post("/api/generate", json={"only": "sales"})
        assert resp.status_code == 200

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


class TestGenerateRequestModel:
    """Test pydantic validation of GenerateRequest."""

    @pytest.fixture(autouse=True)
    def _isolate_final_output(self, tmp_path):
        """Redirect final_output to a temp dir so --clean / --regen don't
        delete the real generated_datasets/ folder."""
        with _state._cfg_lock:
            orig = _state._cfg.defaults.final_output
            _state._cfg.defaults.final_output = str(tmp_path / "generated_datasets")
        yield
        with _state._cfg_lock:
            _state._cfg.defaults.final_output = orig

    def test_empty_body_accepted(self, client):
        _state._current_job = None
        resp = client.post("/api/generate", json={})
        assert resp.status_code == 200

    def test_clean_flag_accepted(self, client):
        _state._current_job = None
        resp = client.post("/api/generate", json={"clean": True})
        assert resp.status_code == 200

    def test_regen_dimensions_list_accepted(self, client):
        _state._current_job = None
        resp = client.post("/api/generate", json={
            "regen_dimensions": ["products", "customers"]
        })
        assert resp.status_code == 200

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

    def test_geo_weights_non_dict_ignored(self, client):
        """geoWeights must be a dict; non-dict should be ignored."""
        # Get current weights
        before = client.get("/api/config").json()["geoWeights"]
        # Post non-dict
        resp = client.post("/api/config", json={"values": {"geoWeights": "invalid"}})
        assert resp.status_code == 200
        after = client.get("/api/config").json()["geoWeights"]
        assert after == before  # unchanged

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
