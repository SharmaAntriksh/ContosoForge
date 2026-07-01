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
* **Multi-chunk runs are NOT worker-count invariant** — customer *discovery* reads
  and mutates a per-worker ``State.seen_customers`` accumulator across chunks
  (``sales_logic/chunk_builder.py`` ~1421/1913). Because chunks are dispatched via
  ``imap_unordered`` with no chunk→worker affinity, the set of "already discovered"
  customers a chunk sees depends on which other chunks landed on the same worker —
  so ``--workers 1`` and ``--workers 4`` produce different customer assignments.
  This is review Finding #5/#6, fixed by Phase 1.1 (closed-form, hash-seeded
  discovery month). The test below is ``xfail(strict=True)`` today; when Phase 1.1
  lands it XPASSes, the strict marker turns that into a failure, and whoever fixes
  it deletes the marker — making this a permanent hard regression guard.

Notes:
* Customer discovery cannot be disabled purely via config — an auto-adjust in
  ``sales.generate_sales_fact`` (~line 2436) raises ``new_customer_share`` above 0
  whenever undiscovered customers exist — so the single-chunk case is used as the
  "discovery-on but still invariant" control instead of a discovery-off run.
* The divergence is chunk-count sensitive: at ~6 chunks discovery saturates within
  each worker's slice and the arrangements reconverge; ≥8 chunks reliably diverges.
  We use 12 chunks, which diverged on 3/3 runs (~93% of rows) while the single-chunk
  control stayed identical.

These runs spawn a real multiprocessing pool and generate a small dataset, so they
are slower than the unit tests (a few seconds each).
"""
from __future__ import annotations

import pytest

pytest.importorskip("yaml")
pytest.importorskip("pandas")
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


@pytest.mark.xfail(
    strict=True,
    reason="Finding #5/#6: customer discovery uses a per-worker seen_customers "
           "accumulator read across chunks, so multi-chunk output depends on worker "
           "count / scheduling. Fixed by Phase 1.1 (closed-form discovery month). "
           "When that lands this XPASSes — delete this marker to turn it into a hard "
           "regression guard.",
)
def test_multi_chunk_output_independent_of_worker_count(run_sales):
    """The advertised guarantee: identical output regardless of --workers.

    Currently FAILS because multi-chunk customer discovery is worker-dependent.
    """
    digest_w1 = run_sales("multi_w1", workers=1, chunk_size=MULTI_CHUNK_SIZE)
    digest_w4 = run_sales("multi_w4", workers=4, chunk_size=MULTI_CHUNK_SIZE)
    assert digest_w1 == digest_w4, (
        "Sales fact depends on worker count: --workers 1 and --workers 4 produced "
        "different data for the same config + seed (customer discovery is not "
        "reproducible across chunk→worker scheduling)."
    )
