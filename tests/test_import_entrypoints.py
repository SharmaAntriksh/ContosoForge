"""Entrypoint orchestration for --identifier-case: temp-dir re-emit plumbing,
cleanup, and friendly failures. No database required: the importer functions are
monkeypatched."""
from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_REPO_CONFIG = REPO_ROOT / "config.yaml"


def _load_entrypoint(module_name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(module_name, REPO_ROOT / rel_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _make_run(tmp_path: Path) -> Path:
    """Minimal packaged CSV run satisfying both entrypoints' _resolve_run_dir."""
    run = tmp_path / "run"
    (run / "config").mkdir(parents=True)
    shutil.copyfile(_REPO_CONFIG, run / "config" / "config.yaml")
    (run / "dimensions").mkdir()
    (run / "dimensions" / "Customers.csv").write_text("CustomerKey\n1\n", encoding="utf-8")
    (run / "facts" / "sales").mkdir(parents=True)
    (run / "facts" / "sales" / "sales.csv").write_text("SalesKey\n1\n", encoding="utf-8")
    for d in ("sql/sqlserver/schema", "sql/sqlserver/load", "sql/postgres/schema", "sql/postgres/load"):
        (run / d).mkdir(parents=True)
    return run


@pytest.mark.skipif(not _REPO_CONFIG.is_file(), reason="repo config.yaml not present")
class TestSqlServerEntrypoint:
    def _main(self, monkeypatch, run: Path, extra_args: list[str], stub) -> int:
        mod = _load_entrypoint("rssi_under_test", "scripts/sql/run_sql_server_import.py")
        monkeypatch.setattr(mod, "import_sql_server", stub)
        # Don't install real signal handlers under pytest (they mutate global state).
        monkeypatch.setattr(mod, "_install_graceful_stop", lambda: None)
        argv = ["prog", "--server", "s", "--database", "d", "--trusted",
                "--run-path", str(run), *extra_args]
        monkeypatch.setattr(sys, "argv", argv)
        return mod.main()

    def test_snake_reemits_into_temp_and_cleans_up(self, monkeypatch, tmp_path: Path) -> None:
        run = _make_run(tmp_path)
        seen = {}

        def stub(**kwargs):
            root = kwargs["sql_root_override"]
            seen["override"] = root
            seen["ddl_exists_during_import"] = (
                root is not None
                and (Path(root) / "sqlserver" / "schema" / "01_create_dimensions.sql").is_file()
            )

        rc = self._main(monkeypatch, run, ["--identifier-case", "snake"], stub)
        assert rc == 0
        assert seen["override"] is not None
        assert seen["ddl_exists_during_import"] is True
        assert not Path(seen["override"]).exists()  # temp dir cleaned after main()

    def test_pascal_passes_no_override(self, monkeypatch, tmp_path: Path) -> None:
        run = _make_run(tmp_path)
        seen = {}

        def stub(**kwargs):
            seen["override"] = kwargs["sql_root_override"]

        rc = self._main(monkeypatch, run, [], stub)
        assert rc == 0
        assert seen["override"] is None

    def test_missing_packaged_config_exits_2_with_message(self, monkeypatch, tmp_path: Path, capsys) -> None:
        run = _make_run(tmp_path)
        (run / "config" / "config.yaml").unlink()

        def stub(**kwargs):
            raise AssertionError("importer must not be reached")

        rc = self._main(monkeypatch, run, ["--identifier-case", "snake"], stub)
        assert rc == 2
        assert "VALIDATION ERROR" in capsys.readouterr().err


@pytest.mark.skipif(not _REPO_CONFIG.is_file(), reason="repo config.yaml not present")
class TestPostgresEntrypoint:
    def _main(self, monkeypatch, run: Path, extra_args: list[str], stub) -> int:
        mod = _load_entrypoint("rpgi_under_test", "scripts/sql/run_postgres_import.py")
        monkeypatch.setattr(mod, "import_postgres", stub)
        argv = ["prog", "--database", "d", "--run-path", str(run), *extra_args]
        monkeypatch.setattr(sys, "argv", argv)
        return mod.main()

    def test_snake_reemits_into_temp_and_cleans_up(self, monkeypatch, tmp_path: Path) -> None:
        run = _make_run(tmp_path)
        seen = {}

        def stub(**kwargs):
            seen["override"] = kwargs["sql_root_override"]

        rc = self._main(monkeypatch, run, ["--identifier-case", "snake"], stub)
        assert rc == 0
        assert seen["override"] is not None
        assert not Path(seen["override"]).exists()

    def test_pascal_passes_no_override(self, monkeypatch, tmp_path: Path) -> None:
        run = _make_run(tmp_path)
        seen = {}

        def stub(**kwargs):
            seen["override"] = kwargs["sql_root_override"]

        rc = self._main(monkeypatch, run, [], stub)
        assert rc == 0
        assert seen["override"] is None

    def test_missing_packaged_config_exits_2_with_message(self, monkeypatch, tmp_path: Path, capsys) -> None:
        run = _make_run(tmp_path)
        (run / "config" / "config.yaml").unlink()

        def stub(**kwargs):
            raise AssertionError("importer must not be reached")

        rc = self._main(monkeypatch, run, ["--identifier-case", "snake"], stub)
        assert rc == 2
        assert "FAIL" in capsys.readouterr().err
