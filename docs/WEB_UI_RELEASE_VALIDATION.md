# Web UI release validation

This checklist verifies the beginner and advanced web experience without using
or modifying a developer's working configuration, scratch data, dimension
versions, or generated datasets.

## Isolated generation gate

Run:

```powershell
.\.venv\Scripts\python.exe scripts\validate_web_release.py
```

The validator creates a temporary `git archive` of `HEAD`, writes bounded
release-only configuration into that copy, and runs 500-row CSV, Parquet, and
Delta generations with one worker. Each generated configuration is dry-run
validated before generation.

For each format the validator checks:

- the packaged `config.yaml` and `models.yaml` snapshots exist;
- the expected CSV, Parquet, or Delta artifacts exist;
- CSV includes generated SQL scripts;
- Delta includes transaction logs;
- the first and last five sales rows can be previewed;
- both `/api` and `/v1/api` preview routes behave identically.

The temporary checkout is deleted after a successful run. Use `--keep` only
when you need to inspect a failed or completed validation workspace:

```powershell
.\.venv\Scripts\python.exe scripts\validate_web_release.py --keep
```

Validate only one format while debugging:

```powershell
.\.venv\Scripts\python.exe scripts\validate_web_release.py --format parquet
```

## Automated release gates

Run the project and focused web suites:

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m pytest `
  tests/test_release_validator.py `
  tests/test_web_api.py `
  tests/test_web_routes.py `
  tests/test_web_frontend.py `
  tests/test_web_data_preview.py
```

Run source and frontend dependency checks:

```powershell
.\.venv\Scripts\python.exe -m ruff check .
uv pip check --python .venv\Scripts\python.exe
Push-Location web\frontend
npm ci
npm run build:check
npm audit
Pop-Location
```

Record the working-tree status before and after these commands. Only
pre-existing, explicitly known local changes may remain.

## Browser acceptance

Use a clean browser profile and the production frontend bundle.

1. Launch `.\scripts\run_web.ps1`.
2. Complete Quick Start using Quick sample, a short date range, Parquet, and
   Core retail.
3. Confirm Review explains that settings are session-only and does not imply
   the YAML files will be overwritten.
4. Apply, generate, reload during the run, and confirm progress reconnects.
5. Cancel a second run and confirm it reaches one terminal cancelled state.
6. Open Data Preview and inspect both the first and final pages of a fact table.
7. Switch to Advanced Setup and confirm form, YAML, Models, and SQL Import
   workflows remain available.
8. Repeat at 1440, 1024, 768, 390, and 320 CSS pixels, plus a 200%-zoom
   equivalent viewport.
9. Navigate every control by keyboard and inspect the accessibility tree for
   named controls, live status, validation summary, and pipeline log regions.
10. Stop the server and confirm no generation process remains.

## Failure-path acceptance

The focused route suite covers malformed values, validation failures, start
conflicts, cancellation, timeout, reload recovery, bounded logs, process-tree
termination, and server shutdown. Before release, repeat one controlled live
cancellation and server-shutdown check on Windows because those paths depend on
native process-group and Job Object behavior.

Database import must remain non-destructive during release validation. Treat
successful CSV SQL-bundle generation as the default dry run; use a disposable
database only when a live importer test is explicitly planned.

## July 29, 2026 baseline

- Project suite before the final browser finding: `2426 passed` in `153.71s`.
- Focused web, preview, and release-validator suite before the final browser
  finding: `197 passed` in `6.27s`.
- Isolated Parquet: 500 sales rows, 22 Parquet files, first/last preview passed
  through `/api` and `/v1/api`.
- Isolated CSV: 500 sales rows, 22 CSV files and 18 SQL scripts.
- Isolated Delta: 500 sales rows, 24 Parquet data files with Delta transaction
  logs. This gate exposed a partitioned-preview failure: physical Parquet
  fragments could not project Delta's virtual `Year` and `Month` columns. The
  fragment reader now receives the complete dataset schema, and first/last
  previews pass with both partition values present.
- Ruff passed for the new release validator and tests.
- The only working-tree changes present before validation were the developer's
  local `config.yaml` and FX master changes; the gates added no generated or
  versioning files to the repository.

## Final production-browser and Windows lifecycle gate

The final gate ran from isolated archives of commits `4d0cb71` and `42106de`;
it did not read or modify the developer's working `config.yaml`, FX master,
scratch data, dimension versions, or generated datasets.

Browser coverage:

- The production bundle had no document-level horizontal overflow at
  `1440x900`, `1024x768`, `768x900`, `390x844`, `320x568`, or the
  `720x450` 200%-zoom equivalent. All visible buttons were at least 24 CSS
  pixels in both dimensions.
- Quick Start completed all six screens with a 100K-row Parquet/Core retail
  configuration. Applying the review changed server memory only, and Advanced
  Setup reflected the same values.
- Data Preview opened the isolated 500-row Parquet package. The first page
  contained rows 1–100 and Last contained rows 401–500.
- SQL Server Import loaded its named connection controls and correctly disabled
  Import when no CSV package was selected. No database connection or write was
  attempted.
- The semantic audit found one `main`, one `h1`, an English document language,
  a valid skip target, no duplicate IDs, no unnamed visible controls, and no
  images missing `alt`. Guided progress and application navigation are named
  landmarks; pipeline feedback uses live status and log regions.
- The mobile drawer closes with Escape and returns focus to Open navigation.
  The browser console reported no warnings or errors.

Live lifecycle coverage:

- A 10M-row run was reloaded while active. The fresh page restored
  `Running...`, elapsed time, retained log output, and the cancellation action.
- A 100M-row run was cancelled through the production UI. The API reached the
  single terminal `cancelled` state and Windows showed no remaining generator
  or worker process.
- This check exposed one release defect: the accessible log status said
  `Completed` after cancellation. Commit `42106de` preserves terminal job
  status, announces `Cancelled`, `Timed out`, or `Failed` accurately, and adds
  Escape/focus-return behavior to application navigation.
- The corrected production bundle was rebuilt and retested. Cancellation was
  announced as `Cancelled`, with the cancellation message retained in the
  pipeline log.
- The server was then stopped while a separate 100M-row run was active. The
  uvicorn process, generator process, and workers all exited, and port `8512`
  no longer had a listener.

Post-fix gates:

- Project suite: `2428 passed, 1 warning in 195.01s`.
- Focused web, preview, and release-validator suite:
  `199 passed, 1 warning in 7.37s`.
- Frontend contract tests: `17 passed`.
- Production build freshness: passed (`b27be522099f`).
- `npm audit`: `0 vulnerabilities`.
- `uv pip check`: all 60 installed packages compatible.
- Ruff and `git diff --check`: passed.
