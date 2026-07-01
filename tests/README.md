# Tests

Run the suite with the `dev` extra (pytest lives there; the `.venv` is uv-managed):

```bash
uv run --extra dev pytest                    # whole suite
uv run --extra dev pytest tests/test_dates.py -k "iso"   # a subset
```

`pyproject.toml` sets `-v --tb=short`. Web-API tests need `httpx` (skipped otherwise);
the `test_postgres_*` tests exercise the PostgreSQL dialect/import path.

---

## Sales-fact determinism & schema guardrails (Phase 0 of the improvement plan)

These encode guarantees the project advertises but that were previously untested.

| File | Asserts | Today | Flips green when |
|---|---|---|---|
| `test_sales_determinism_workers.py` | sales fact is identical regardless of `--workers` | **all pass** (multi-chunk now a hard guard) | ✅ **Phase 1.1 landed** (closed-form discovery) |
| `test_sales_determinism_chunksize.py` | sales fact shape is independent of `chunk_size` | 1 pass, 1 **xfail** (per-month distinct) | **Phase 2** (global per-month plan) |
| `test_sales_schema_consistency.py` | Arrow (parquet) and SQL (DDL) Sales schemas agree | 2 pass, 1 **xfail** (exact dtypes) ×params | **Phase 5.5** (one canonical schema) |

`test_sales_determinism_workers.py` also carries `test_multi_chunk_worker_invariance_second_seed`
(seed 20250701) — a second seed that exercises the worker-lifetime product-CDF cache path the
Phase 1.1 stress uncovered (the cache was keyed by calendar month but its brand mix depends on the
absolute month). Keep it: seed 1234 alone does not trigger that path.

The `_workers`/`_chunksize` tests run the real pipeline on a tiny dataset (a few
seconds each, spawn a worker pool). `test_sales_schema_consistency.py` is a fast
pure-unit check (no generation).

### The xfail → hard-assert protocol

Each guardrail's *desired* behaviour is asserted directly, but the current (buggy)
reality is marked `@pytest.mark.xfail(strict=True)`. `strict=True` means:

- **Today:** the bug is present, the assertion fails, pytest reports `XFAIL` (green).
- **After the fixing phase lands:** the assertion passes, pytest reports `XPASS`, and
  because it is *strict* that XPASS is a **test failure**.

That failure is the signal. When it fires, **delete the `@pytest.mark.xfail(...)`
marker** (and its `reason`) so the test becomes a permanent hard regression guard.
Do not weaken it to `strict=False` or delete the test. The marker's `reason` names the
phase and finding responsible.

---

## When a change intentionally alters generated output

Phases 1–4 of the plan deliberately change the bytes of the generated sales fact
(discovery scheduling, chunk planning, demand/pricing models). Expect:

- The determinism guardrails above to **XPASS** as their phase lands → remove the
  `xfail` marker (see protocol above).
- No stored "golden" fixtures to update: the current guardrails compare two
  *in-process* runs to each other (worker-count vs worker-count, chunk-size vs
  chunk-size), so there is nothing on disk to regenerate.

If a future test needs a **stored golden dataset** (e.g. a committed reference
digest), build and regenerate it deterministically with the shared harness in
[`tests/sales_gen.py`](sales_gen.py) rather than hand-rolling generation:

```python
# regenerate a reference sales fact + its canonical digest
from pathlib import Path
from tests import sales_gen

base = Path("scratch_ref")
dims = base / "dims"; dims.mkdir(parents=True, exist_ok=True)
cfg = sales_gen.small_config(dims_dir=dims, scratch_dir=base / "s",
                             final_dir=base / "f", workers=1, chunk_size=1000)
sales_gen.run_pipeline_stage(base, cfg, only="dimensions")
sales_gen.run_pipeline_stage(base, cfg, only="sales")
df = sales_gen.load_sales(base / "f", base / "s")
print(sales_gen.sales_digest(df))   # commit this as the new golden digest
```

`sales_gen` bases its config on the repo's real `config.yaml`/`models.yaml` (patched
small) so it stays representative, and uses a fixed seed — the digest is stable as
long as the generation logic is unchanged.
