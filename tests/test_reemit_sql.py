"""Import-time re-emit: reproduce a run's SQL in a chosen identifier case.

``reemit_run_sql`` is what the importers call so one PascalCase-generated dataset
can be imported as either pascal or snake without regenerating data. It reads the
run's own packaged config + data and drives the same roster generators packaging
uses. These tests build a minimal CSV run and assert the two cases render
correctly (snake identifiers everywhere + verification skipped; pascal identity +
verification present).
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from src.engine.packaging.reemit_sql import reemit_run_sql

REPO_ROOT = Path(__file__).resolve().parents[1]
_REPO_CONFIG = REPO_ROOT / "config.yaml"


def _make_run(tmp_path: Path) -> Path:
    """A minimal packaged CSV run: config + one dim CSV + one fact CSV."""
    run = tmp_path / "run"
    (run / "config").mkdir(parents=True)
    shutil.copyfile(_REPO_CONFIG, run / "config" / "config.yaml")

    dims = run / "dimensions"
    dims.mkdir()
    (dims / "Customers.csv").write_text("CustomerKey,CustomerID\n1,1\n", encoding="utf-8")

    sales = run / "facts" / "sales"
    sales.mkdir(parents=True)
    (sales / "sales.csv").write_text("SalesKey\n1\n", encoding="utf-8")
    return run


def _write_manifest(run: Path, **overrides) -> None:
    manifest = {
        "schema_version": 1,
        "order_id_int64": None,
        "total_rows": 1000,
        "skip_order_cols": False,
        "sales_output": "sales",
    }
    manifest.update(overrides)
    (run / "config" / "sql_generation.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )


def _ss_create_dims(sql_root: Path) -> str:
    return (sql_root / "sqlserver" / "schema" / "01_create_dimensions.sql").read_text(encoding="utf-8")


def _ss_verify(sql_root: Path) -> Path:
    return sql_root / "sqlserver" / "schema" / "05_create_verify_schema.sql"


@pytest.mark.skipif(not _REPO_CONFIG.is_file(), reason="repo config.yaml not present")
class TestReemitSnake:
    def test_snake_casts_identifiers(self, tmp_path: Path) -> None:
        run = _make_run(tmp_path)
        out = tmp_path / "out"
        reemit_run_sql(run_dir=run, out_sql_root=out, case="snake")

        ddl = _ss_create_dims(out)
        assert "[customers]" in ddl and "[customer_key]" in ddl
        assert "[Customers]" not in ddl and "[CustomerKey]" not in ddl

    def test_snake_skips_verification(self, tmp_path: Path) -> None:
        run = _make_run(tmp_path)
        out = tmp_path / "out"
        reemit_run_sql(run_dir=run, out_sql_root=out, case="snake")
        assert not _ss_verify(out).exists()

    def test_snake_postgres_views_quoted_snake(self, tmp_path: Path) -> None:
        run = _make_run(tmp_path)
        out = tmp_path / "out"
        reemit_run_sql(run_dir=run, out_sql_root=out, case="snake")
        views = (out / "postgres" / "schema" / "04_create_views.sql").read_text(encoding="utf-8")
        assert '"customers"' in views
        assert '"Customers"' not in views


@pytest.mark.skipif(not _REPO_CONFIG.is_file(), reason="repo config.yaml not present")
class TestReemitPascal:
    def test_pascal_is_identity(self, tmp_path: Path) -> None:
        run = _make_run(tmp_path)
        out = tmp_path / "out"
        reemit_run_sql(run_dir=run, out_sql_root=out, case="pascal")

        ddl = _ss_create_dims(out)
        assert "[Customers]" in ddl and "[CustomerKey]" in ddl
        assert "[customers]" not in ddl

    def test_pascal_keeps_verification(self, tmp_path: Path) -> None:
        run = _make_run(tmp_path)
        out = tmp_path / "out"
        reemit_run_sql(run_dir=run, out_sql_root=out, case="pascal")
        assert _ss_verify(out).is_file()


def _ss_facts_ddl(sql_root: Path) -> str:
    return (sql_root / "sqlserver" / "schema" / "02_create_facts.sql").read_text(encoding="utf-8")


@pytest.mark.skipif(not _REPO_CONFIG.is_file(), reason="repo config.yaml not present")
class TestReemitManifest:
    def test_manifest_order_id_int64_wins_over_config_rows(self, tmp_path: Path) -> None:
        # config.yaml says a small row count, but the manifest's authoritative
        # width flag must force BIGINT regardless of the row-count estimate.
        import re

        run = _make_run(tmp_path)
        _write_manifest(run, order_id_int64=True, total_rows=1000)
        out = tmp_path / "out"
        reemit_run_sql(run_dir=run, out_sql_root=out, case="snake")
        ddl = _ss_facts_ddl(out)
        assert re.search(r"order_number\W+BIGINT", ddl, re.IGNORECASE), ddl

    def test_manifest_skip_order_cols_shapes_sales_and_disables_returns(self, tmp_path: Path) -> None:
        import re

        run = _make_run(tmp_path)
        _write_manifest(run, skip_order_cols=True, sales_output="sales")
        out = tmp_path / "out"
        reemit_run_sql(run_dir=run, out_sql_root=out, case="snake")
        ddl = _ss_facts_ddl(out)
        # Scope the OrderNumber check to the Sales table block. Other facts (e.g.
        # complaints) legitimately carry their own OrderNumber column regardless
        # of sales.skip_order_cols.
        blocks = re.split(r"(CREATE TABLE \[dbo\]\.\[[^\]]+\])", ddl)
        sales_body = ""
        for i in range(1, len(blocks), 2):
            if blocks[i].endswith("[sales]"):
                sales_body = blocks[i + 1] if i + 1 < len(blocks) else ""
                break
        assert sales_body, "sales CREATE TABLE not found"
        assert "order_number" not in sales_body.lower()
        # returns auto-disable (skip_order_cols + sales_output=sales) drops the table.
        assert "sales_returns" not in ddl.lower()

    def test_missing_manifest_warns_and_still_renders(self, tmp_path: Path, capsys) -> None:
        run = _make_run(tmp_path)  # no sql_generation.json
        out = tmp_path / "out"
        reemit_run_sql(run_dir=run, out_sql_root=out, case="snake")
        assert (out / "sqlserver" / "schema" / "01_create_dimensions.sql").is_file()
        captured = capsys.readouterr()
        assert "sql_generation.json" in (captured.out + captured.err)

    def test_missing_config_yaml_raises_packaging_error(self, tmp_path: Path) -> None:
        from src.exceptions import PackagingError

        run = _make_run(tmp_path)
        (run / "config" / "config.yaml").unlink()
        with pytest.raises(PackagingError):
            reemit_run_sql(run_dir=run, out_sql_root=tmp_path / "out", case="snake")

    def test_snake_load_scripts_use_snake_targets_and_run_paths(self, tmp_path: Path) -> None:
        run = _make_run(tmp_path)
        out = tmp_path / "out"
        reemit_run_sql(run_dir=run, out_sql_root=out, case="snake")
        load_dir = out / "sqlserver" / "load"
        text = "\n".join(p.read_text(encoding="utf-8") for p in sorted(load_dir.glob("*.sql")))
        assert text, "no sqlserver load scripts were re-emitted"
        assert "[customers]" in text and "[Customers]" not in text
        # Load scripts embed the run's own CSV paths, not the temp/out dir's.
        dims = str(run / "dimensions")
        assert dims in text or dims.replace("\\", "/") in text or dims.replace("/", "\\") in text
