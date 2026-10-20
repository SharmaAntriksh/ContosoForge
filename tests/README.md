# Tests

Install the locked development tools from the repo root:

```powershell
uv sync --locked --extra dev
.venv/Scripts/python.exe -m pytest
```

On POSIX use `.venv/bin/python`. `pyproject.toml` sets `-v --tb=short`.
Use `--collect-only -q` to discover current tests; keep this guide focused on
contracts rather than test counts or temporary implementation phases.
`httpx` comes with the dev extra so web tests do not silently skip. Both database
Python drivers are default dependencies. Importer unit tests use fake connections;
live database connectivity, permissions, and server-side file access need a
separate integration run when that is the task.

## Choose checks by the changed behavior

All filenames in the table are under `tests/`. Pass the relevant files to
`.venv/Scripts/python.exe -m pytest`.

| Change | Useful test groups |
|---|---|
| Config/coercion | `test_config_loader.py`, `test_schema.py`, `test_state.py` |
| Sales worker boundary | `test_sales_contract.py`, `test_worker_cfg_validation.py`, `test_state.py` |
| Sales sampling / determinism | `test_sales_determinism_workers.py`, `test_sales_determinism_chunksize.py`, `test_sales_snapshot.py` |
| Temporal mix | `test_temporal_mix.py`, `test_customer_temporal_mix.py`, `test_product_temporal_mix.py`, `test_temporal_mix_calibration.py` |
| Counter-based sampling | `test_product_sampling_counter.py`, `test_channel_time_counter.py`, `test_hashing.py` |
| Sales output schemas / writers | `test_sales_schema_consistency.py`, `test_sales_writer.py`, `test_sales_writer_atomic.py`, `test_delta_writer_schema.py` |
| Dates registry / calendars | `test_dates.py`, `test_dates_snapshot.py` |
| Workforce / temporal coverage | `test_temporal_contracts.py`, `test_workforce_published_contract.py`, `test_store_warehouse_causality.py`, `test_coverage_preflight.py` |
| SQL generation / casing | `test_dialect.py`, `test_dialect_postgres.py`, `test_constraint_generation.py`, `test_view_generation.py`, `test_identifier_case_e2e.py`, `test_reemit_sql.py` |
| Importers / PowerShell | `test_sql_import_hardening.py`, `test_postgres_import.py`, `test_import_entrypoints.py`, `test_powershell_driver_preflight.py` |
| Web routes / preview / import | `test_web_api.py`, `test_web_routes.py`, `test_web_data_preview.py`, `test_web_import_routes.py` |
| Frontend build and serving | `test_web_frontend.py`; also `npm run build:check` from `web/frontend/` |
| Packaging / published output | `test_package_output.py`, `test_packaging.py`, `test_published_data_conformance.py`, `test_analytical_quality.py` |
| Benchmark / release tooling | `test_benchmark_release.py`, `test_release_manifest.py`, `test_release_validator.py` |

Use the relevant area skill for additional focused tests; this table is not the
entire suite. PowerShell tests run Windows PowerShell 5.1 and PowerShell 7 when
available, skip unavailable shells, and mock installation rather than changing
the developer's environment.

## Sales determinism and stored snapshots

These are different checks:

- **Worker invariance:** fixed seed/config/chunk size, different worker counts,
  identical canonical sales fingerprints. Keep the second seed `20250701` in
  `test_sales_determinism_workers.py`; it exercises a cache path seed 1234 misses.
- **Chunk-shape invariance:** row totals, monthly row totals, and monthly distinct
  customer coverage agree across chunk sizes. Full row multiset identity is not
  asserted; chunk-sharded order labels and chunk-local draws may differ.
- **Absolute fingerprints:** `test_sales_snapshot.py` checks each fixture/worker/
  chunk-size cell against `tests/_snapshots/sales_fact_fingerprints.json`.
  The fingerprint covers schema, count, and key-sorted LF-normalized values;
  it is not a hash of physical parquet files.

Reuse `tests/sales_gen.py` (`small_config`, `run_pipeline_stage`, `load_sales`)
for small pipeline tests. It reads the real YAML defaults, patches scale and
paths, and supplies fixed seeds. Use temporary output/dimension directories and
isolate `src.versioning.version_store.VERSION_DIR` when exercising versioning;
do not overwrite the developer's dimension cache or generated datasets.

A behavior-preserving edit must leave stored fingerprints unchanged. When an
intentional generation change moves output, first inspect the affected cells
and explain the change. Regenerate the sales baseline with:

```powershell
$env:REGEN_SALES_SNAPSHOT = "1"
try { .venv/Scripts/python.exe -m pytest tests/test_sales_snapshot.py -q }
finally { Remove-Item Env:REGEN_SALES_SNAPSHOT }
```

Restore the previous value instead if the caller already set that environment
variable. POSIX equivalent:

```bash
REGEN_SALES_SNAPSHOT=1 .venv/bin/python -m pytest tests/test_sales_snapshot.py -q
```

Commit the explained baseline diff with the behavior change. Dates and store
cluster snapshots have their own fixture/regen mechanisms; inspect
`test_dates_snapshot.py` and `test_store_cluster_snapshot.py` before updating
`tests/golden/dates_snapshots.json` or
`tests/_snapshots/store_cluster_fingerprints.json`.

Some historical regressions used strict `xfail` markers. Inspect the current
markers rather than assuming an old phase plan is still active. If a fixed
behavior produces strict XPASS, remove that marker and retain the assertion.

## Release checks versus working-tree checks

`scripts/validate_web_release.py` validates an isolated archive of **committed
HEAD**. Run local pytest/build checks to validate uncommitted work. The release
validator can be useful after committing, but cannot certify pending edits.
`scripts/benchmark_output_modes.py` defaults to a 20M-row workload; provide
explicit small `--rows` and `--workers` values for a smoke benchmark.
