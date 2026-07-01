"""Phase 0.2 guardrail — sales output must be independent of chunk_size.

``chunk_size`` is documented (CLAUDE.md) as a pure performance-tuning knob:
"too small = overhead, too large = memory pressure (default 1M is good)". It must
NOT change the shape of the generated data. These tests fix ``--workers 1`` (so the
only variable is ``chunk_size``, with none of the worker-scheduling nondeterminism
that Phase 0.1 covers) and compare a single-chunk run against a many-chunk run of
the same config + seed.

What they pin down (verified empirically while authoring):

* **Total row count is chunk-size invariant** — both runs emit exactly the
  requested ``total_rows``. (passes today)
* **The per-month distinct-customer curve is chunk-size invariant** (Phase 2). It
  used *not* to be: the per-month distinct target was computed against *per-chunk*
  rows and repeats were drawn only from a chunk-local pool, so splitting the same
  rows into more chunks redistributed which customers transacted in which month —
  ``base_distinct_ratio`` (a business parameter) silently depended on ``chunk_size``
  (review Finding #4/#14). Phase 2 fixes this: the coordinator computes the
  per-month rows, orders, and distinct target ONCE against the global month totals
  and each chunk slices a contiguous band of every month's order-id space
  (``chunk_builder._chunk_month_band`` + ``assign_orders_to_customers``), so the
  per-month distinct-customer set is exactly the month pool regardless of chunking.
  Now a hard regression guard (the ``xfail`` was removed when Phase 2 landed).

Because both runs are single-worker they are fully deterministic, so any regression
would be reproducible run-to-run (no scheduling flakiness): before Phase 2, at 12
chunks the per-month distinct-customer counts differed from the single-chunk run by
up to ~48 customers per month while total rows and total distinct customers were
unchanged.
"""
from __future__ import annotations

import pytest

pytest.importorskip("yaml")
pytest.importorskip("pandas")
pytest.importorskip("pyarrow.parquet")

from tests import sales_gen

TOTAL_ROWS = sales_gen.DEFAULT_TOTAL_ROWS   # 12_000
SINGLE_CHUNK_SIZE = TOTAL_ROWS              # -> 1 chunk (the reference partition)
MANY_CHUNK_SIZE = 1_000                     # -> 12 chunks


@pytest.fixture(scope="module")
def run_sales_df(tmp_path_factory):
    """Generate dimensions once; return ``run(name, *, chunk_size) -> DataFrame``.

    Always single-worker so ``chunk_size`` is the only independent variable.
    """
    base = tmp_path_factory.mktemp("sales_chunksize")
    dims_dir = base / "dims"
    dims_dir.mkdir()

    dims_cfg = sales_gen.small_config(
        dims_dir=dims_dir, scratch_dir=base / "_dims_scratch",
        final_dir=base / "_dims_final", workers=1, chunk_size=MANY_CHUNK_SIZE,
    )
    sales_gen.run_pipeline_stage(base, dims_cfg, only="dimensions")

    def _run(name: str, *, chunk_size: int):
        run_dir = base / name
        scratch, final = run_dir / "scratch", run_dir / "final"
        scratch.mkdir(parents=True, exist_ok=True)
        final.mkdir(parents=True, exist_ok=True)
        cfg = sales_gen.small_config(
            dims_dir=dims_dir, scratch_dir=scratch, final_dir=final,
            workers=1, chunk_size=chunk_size,
        )
        sales_gen.run_pipeline_stage(run_dir, cfg, only="sales")
        return sales_gen.load_sales(final, scratch)

    return _run


def test_total_rows_invariant_to_chunk_size(run_sales_df):
    """The requested row count is honoured regardless of chunk_size."""
    df_single = run_sales_df("rows_single", chunk_size=SINGLE_CHUNK_SIZE)
    df_many = run_sales_df("rows_many", chunk_size=MANY_CHUNK_SIZE)
    assert len(df_single) == len(df_many) == TOTAL_ROWS, (
        f"Row count depends on chunk_size: single-chunk={len(df_single)}, "
        f"many-chunk={len(df_many)}, requested={TOTAL_ROWS}."
    )


def test_per_month_distinct_customers_invariant_to_chunk_size(run_sales_df):
    """The per-month distinct-customer curve must not depend on chunk_size.

    Hard regression guard since Phase 2 (global per-month plan). Finding #4/#14
    used to make ``base_distinct_ratio`` depend on ``chunk_size`` because the
    distinct target was evaluated against per-chunk rows and repeats were drawn
    from a chunk-local pool. The coordinator now computes the per-month rows,
    orders, and distinct target ONCE against the global month totals; each chunk
    slices a contiguous band of every month's order-id space
    (``chunk_builder._chunk_month_band`` + ``assign_orders_to_customers``), so a
    given global order index always maps to the same customer and the per-month
    distinct-customer set is exactly the month pool regardless of chunking.
    """
    df_single = run_sales_df("dist_single", chunk_size=SINGLE_CHUNK_SIZE)
    df_many = run_sales_df("dist_many", chunk_size=MANY_CHUNK_SIZE)

    pm_single = sales_gen.per_month_distinct_customers(df_single)
    pm_many = sales_gen.per_month_distinct_customers(df_many)

    months = sorted(set(pm_single.index) | set(pm_many.index))
    single = pm_single.reindex(months, fill_value=0)
    many = pm_many.reindex(months, fill_value=0)

    max_abs_diff = int((single - many).abs().max())
    assert max_abs_diff == 0, (
        "Per-month distinct-customer count depends on chunk_size (max monthly "
        f"difference = {max_abs_diff} customers between a 1-chunk and a "
        f"{TOTAL_ROWS // MANY_CHUNK_SIZE}-chunk run of the same config + seed)."
    )
