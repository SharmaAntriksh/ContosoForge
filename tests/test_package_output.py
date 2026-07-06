"""End-to-end packaging contract for the sales fact.

The individual packager helpers (``copy_parquet_facts`` / ``copy_delta_facts`` /
``copy_csv_facts`` and their path probes) are unit-tested in ``test_packaging``,
but nothing drove ``package_output`` through a *real* sales run and pinned the
resulting packaged ``facts/`` tree. That gap matters: packaging historically
*re-discovered* where the sales writer put each table by probing a list of
candidate paths, so a layout change in the writer could silently ship the wrong
file (or nothing) without any test noticing.

This harness runs the actual pipeline (dimensions → sales → packaging) on a tiny
deterministic dataset for each output format and asserts the packaged fact tree.
It is the regression net for threading the run manifest into packaging: the
packaged tree must stay identical whether packaging probes for files or reads
their locations from the manifest.

These runs spawn a real multiprocessing pool, so they are slower than the unit
tests (a few seconds each).
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("yaml")
pytest.importorskip("pandas")
import pyarrow.parquet as pq  # noqa: E402

from tests import sales_gen  # noqa: E402

_SALES_MARKER_COLS = {"UnitPrice", "CustomerKey", "ProductKey"}


def _make_cfg(dims_dir, scratch, final, *, fmt, sales_output="sales", returns=False):
    """A small_config patched to a specific output format / sales mode."""
    cfg = sales_gen.small_config(
        dims_dir=dims_dir, scratch_dir=scratch, final_dir=final,
        workers=2, chunk_size=1_000,
    )
    cfg["sales"]["file_format"] = fmt
    cfg["sales"]["sales_output"] = sales_output
    if returns:
        cfg.setdefault("returns", {})["enabled"] = True
        cfg["returns"].setdefault("return_rate", 0.2)
    return cfg


def _packaged_facts_dir(final_dir: Path) -> Path:
    """The ``facts/`` folder inside the single timestamped run folder."""
    candidates = [d for d in final_dir.iterdir() if d.is_dir() and (d / "facts").is_dir()]
    assert len(candidates) == 1, f"expected exactly one packaged run folder, found {candidates}"
    return candidates[0] / "facts"


def _is_sales_parquet(path: Path) -> bool:
    try:
        return _SALES_MARKER_COLS <= set(pq.read_schema(path).names)
    except Exception:
        return False


@pytest.fixture(scope="module")
def dims_dir(tmp_path_factory):
    """Generate dimensions once; every format run reuses them."""
    base = tmp_path_factory.mktemp("pkg_dims")
    ddir = base / "dims"
    ddir.mkdir()
    dims_cfg = sales_gen.small_config(
        dims_dir=ddir, scratch_dir=base / "_s", final_dir=base / "_f",
        workers=1, chunk_size=1_000,
    )
    sales_gen.run_pipeline_stage(base, dims_cfg, only="dimensions")
    return ddir


def _run(dims_dir, tmp_path, *, fmt, sales_output="sales", returns=False) -> Path:
    scratch, final = tmp_path / "scratch", tmp_path / "final"
    scratch.mkdir(parents=True, exist_ok=True)
    final.mkdir(parents=True, exist_ok=True)
    cfg = _make_cfg(dims_dir, scratch, final, fmt=fmt,
                    sales_output=sales_output, returns=returns)
    sales_gen.run_pipeline_stage(tmp_path, cfg, only="sales")
    return _packaged_facts_dir(final)


class TestParquetPackaging:
    def test_sales_mode_ships_merged_fact(self, dims_dir, tmp_path):
        facts = _run(dims_dir, tmp_path, fmt="parquet", sales_output="sales")
        merged = facts / "sales.parquet"
        assert merged.is_file(), f"missing packaged sales.parquet under {facts}"
        assert _is_sales_parquet(merged)

    def test_both_mode_with_returns_ships_all_tables(self, dims_dir, tmp_path):
        facts = _run(dims_dir, tmp_path, fmt="parquet",
                     sales_output="both", returns=True)
        for name in ("sales.parquet", "order_detail.parquet",
                     "order_header.parquet", "returns.parquet"):
            assert (facts / name).is_file(), f"missing packaged {name} under {facts}"


class TestCSVPackaging:
    def test_sales_mode_ships_csv_chunks(self, dims_dir, tmp_path):
        facts = _run(dims_dir, tmp_path, fmt="csv", sales_output="sales")
        sales_dir = facts / "sales"
        assert sales_dir.is_dir(), f"missing packaged facts/sales/ under {facts}"
        csvs = list(sales_dir.glob("*.csv"))
        assert csvs, f"no CSV chunks packaged under {sales_dir}"


class TestDeltaPackaging:
    def test_sales_mode_ships_delta_table(self, dims_dir, tmp_path):
        pytest.importorskip("deltalake")
        facts = _run(dims_dir, tmp_path, fmt="deltaparquet", sales_output="sales")
        delta_log = facts / "sales" / "_delta_log"
        assert delta_log.is_dir(), f"missing packaged delta _delta_log under {facts}"
        assert list(delta_log.glob("*.json")), "delta log has no transaction files"
