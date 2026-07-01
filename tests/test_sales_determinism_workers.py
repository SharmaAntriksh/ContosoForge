"""Phase 0.1 guardrail — sales output must be independent of worker count.

ContosoForge advertises "deterministic, idempotent datasets": the same config +
seed must yield the same data regardless of how the work is parallelised. These
tests encode that guarantee for the *sales fact* end-to-end (dimensions → sales →
packaging), which the existing unit-level determinism tests never exercised.

What these tests pin down (verified empirically while authoring):

* **Fixed worker count is deterministic** — two ``--workers 1`` runs are
  byte-identical. (passes today)
* **Single-chunk runs are worker-count invariant** — when the whole fact fits in
  one chunk there is no cross-chunk state, so ``--workers 1`` and ``--workers 4``
  agree even with customer discovery active. This proves the harness *and* that
  every mechanism except cross-chunk accumulation is already worker-safe.
  (passes today)
* **Multi-chunk runs are worker-count invariant** — this used to fail: customer
  *discovery* read and mutated a per-worker ``State.seen_customers`` accumulator
  across chunks, so with ``imap_unordered`` (no chunk→worker affinity) the set of
  "already discovered" customers a chunk saw depended on which other chunks landed
  on the same worker, and ``--workers 1`` vs ``--workers 4`` diverged (~93% of
  rows). Phase 1.1 replaced that accumulator with a static, hash-seeded
  ``customer_discovery_month`` schedule computed once in the coordinator and
  broadcast read-only (review Finding #5/#6), so every chunk is now a pure function
  of its own inputs and worker count no longer affects output. This test was
  ``xfail(strict=True)``; when the fix landed it XPASSed and the marker was removed,
  making it the permanent hard regression guard it is now.

Notes:
* Customer discovery cannot be disabled purely via config — an auto-adjust in
  ``sales.generate_sales_fact`` (~line 2438) raises ``new_customer_share`` above 0
  whenever undiscovered customers exist — so the single-chunk case doubles as a
  "discovery-on but still invariant" control alongside the multi-chunk guard.
* 12 chunks (the size that used to reliably expose the ~93%-of-rows divergence) is
  retained so the guard keeps exercising the previously-broken regime.

These runs spawn a real multiprocessing pool and generate a small dataset, so they
are slower than the unit tests (a few seconds each).
"""
from __future__ import annotations

import pytest

pytest.importorskip("yaml")
pd = pytest.importorskip("pandas")
pytest.importorskip("pyarrow.parquet")

from tests import sales_gen

TOTAL_ROWS = sales_gen.DEFAULT_TOTAL_ROWS   # 12_000
CUSTOMERS = sales_gen.DEFAULT_CUSTOMERS     # 4_000
MULTI_CHUNK_SIZE = 1_000        # -> 12 chunks (reliably exposes the cross-worker bug)
SINGLE_CHUNK_SIZE = TOTAL_ROWS  # -> 1 chunk (no cross-chunk state)


@pytest.fixture(scope="module")
def run_sales(tmp_path_factory):
    """Generate dimensions once; return ``run(name, *, workers, chunk_size) -> digest``.

    Each sales run writes to an isolated output dir (packaging cleans scratch and
    moves the merged fact into the timestamped final folder) and returns the
    canonical digest of the resulting sales fact.
    """
    base = tmp_path_factory.mktemp("sales_workers")
    dims_dir = base / "dims"
    dims_dir.mkdir()

    dims_cfg = sales_gen.small_config(
        dims_dir=dims_dir, scratch_dir=base / "_dims_scratch",
        final_dir=base / "_dims_final", workers=1, chunk_size=MULTI_CHUNK_SIZE,
    )
    sales_gen.run_pipeline_stage(base, dims_cfg, only="dimensions")

    def _run(name: str, *, workers: int, chunk_size: int) -> str:
        run_dir = base / name
        scratch, final = run_dir / "scratch", run_dir / "final"
        scratch.mkdir(parents=True, exist_ok=True)
        final.mkdir(parents=True, exist_ok=True)
        cfg = sales_gen.small_config(
            dims_dir=dims_dir, scratch_dir=scratch, final_dir=final,
            workers=workers, chunk_size=chunk_size,
        )
        sales_gen.run_pipeline_stage(run_dir, cfg, only="sales")
        return sales_gen.sales_digest(sales_gen.load_sales(final, scratch))

    return _run


def test_fixed_worker_count_is_deterministic(run_sales):
    """Same config + seed at a fixed worker count → byte-identical sales fact."""
    digest_a = run_sales("det_a", workers=1, chunk_size=MULTI_CHUNK_SIZE)
    digest_b = run_sales("det_b", workers=1, chunk_size=MULTI_CHUNK_SIZE)
    assert digest_a == digest_b, (
        "Two identical single-worker runs produced different sales facts — "
        "the core determinism guarantee is broken."
    )


def test_single_chunk_output_independent_of_worker_count(run_sales):
    """One chunk → no cross-chunk state → worker count is irrelevant (control)."""
    digest_w1 = run_sales("single_w1", workers=1, chunk_size=SINGLE_CHUNK_SIZE)
    digest_w4 = run_sales("single_w4", workers=4, chunk_size=SINGLE_CHUNK_SIZE)
    assert digest_w1 == digest_w4, (
        "Single-chunk sales fact differs between 1 and 4 workers — divergence is no "
        "longer isolated to cross-chunk state; investigate before trusting the "
        "multi-chunk xfail below."
    )


def test_multi_chunk_output_independent_of_worker_count(run_sales):
    """The advertised guarantee: identical multi-chunk output regardless of --workers.

    Hard regression guard for Phase 1.1: the closed-form ``customer_discovery_month``
    schedule makes each chunk a pure function of its own inputs, so 1, 2, and 4
    workers must all produce a byte-identical sales fact for the same config + seed.
    """
    digest_w1 = run_sales("multi_w1", workers=1, chunk_size=MULTI_CHUNK_SIZE)
    digest_w2 = run_sales("multi_w2", workers=2, chunk_size=MULTI_CHUNK_SIZE)
    digest_w4 = run_sales("multi_w4", workers=4, chunk_size=MULTI_CHUNK_SIZE)
    assert digest_w1 == digest_w2 == digest_w4, (
        "Sales fact depends on worker count: --workers 1/2/4 did not all agree for "
        "the same config + seed (digests "
        f"w1={digest_w1[:12]} w2={digest_w2[:12]} w4={digest_w4[:12]}). Customer "
        "discovery must be reproducible across chunk→worker scheduling."
    )


# A second seed whose data actually exercises the brand-mix CDF cache bug that
# the closed-form-discovery fix uncovered: the worker-lifetime product CDF cache
# was keyed by *calendar* month while its brand-aware value depended on the
# *absolute* month, so when month-skipping chunks landed on different workers the
# first chunk to touch a calendar month fixed the wrong brand mix for later
# same-calendar-month months. Seed 1234 above never triggers it (its chunks don't
# skip the relevant months); this seed diverged ~0.2% of rows before the fix.
_CACHE_BUG_SEED = 20250701


def test_multi_chunk_worker_invariance_second_seed(tmp_path_factory):
    """Independent seed + full-frame digest guarding the product-CDF-cache fix."""
    import hashlib

    base = tmp_path_factory.mktemp("sales_workers_seed2")
    dims_dir = base / "dims"
    dims_dir.mkdir()
    dims_cfg = sales_gen.small_config(
        dims_dir=dims_dir, scratch_dir=base / "_ds", final_dir=base / "_df",
        workers=1, chunk_size=MULTI_CHUNK_SIZE, seed=_CACHE_BUG_SEED,
    )
    sales_gen.run_pipeline_stage(base, dims_cfg, only="dimensions")

    def _digest(workers: int) -> str:
        run_dir = base / f"w{workers}"
        scratch, final = run_dir / "s", run_dir / "f"
        scratch.mkdir(parents=True, exist_ok=True)
        final.mkdir(parents=True, exist_ok=True)
        cfg = sales_gen.small_config(
            dims_dir=dims_dir, scratch_dir=scratch, final_dir=final,
            workers=workers, chunk_size=MULTI_CHUNK_SIZE, seed=_CACHE_BUG_SEED,
        )
        sales_gen.run_pipeline_stage(run_dir, cfg, only="sales")
        df = sales_gen.load_sales(final, scratch)
        cols = sorted(df.columns)
        ordered = df[cols].sort_values(cols).reset_index(drop=True)
        h = pd.util.hash_pandas_object(ordered, index=False).values
        return hashlib.sha256(h.tobytes()).hexdigest()

    d1, d3 = _digest(1), _digest(3)
    assert d1 == d3, (
        f"Sales fact depends on worker count for seed {_CACHE_BUG_SEED}: "
        f"w1={d1[:12]} vs w3={d3[:12]}. The product-CDF cache must be keyed by the "
        "absolute month so brand-mix sampling is chunk→worker-order independent."
    )
