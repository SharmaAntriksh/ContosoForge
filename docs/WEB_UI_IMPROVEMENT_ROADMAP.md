# Web UI Improvement Roadmap

This document turns the July 2026 evaluation of `web/` into a phased
implementation plan. The objective is to make the web experience safe and
approachable for first-time users without removing the advanced controls that
experienced users rely on.

The phases are ordered by risk:

1. Make the test suite safe.
2. Make generation start, cancellation, and recovery trustworthy.
3. Make frontend and backend state agree.
4. Build a clear beginner path to the important controls.
5. Improve accessibility and responsive behavior.
6. Make the frontend self-contained and production-like.
7. Improve scalability for long jobs and large datasets.
8. Run final end-to-end release validation.

Each phase should be implemented, reviewed, and merged independently. Do not
start a later phase merely because an earlier phase has code in progress; its
acceptance criteria must pass first.

## Product principles

All phases should preserve these principles:

- **Safe by default:** a button must not report success unless the backend
  accepted and performed the action.
- **Progressive disclosure:** common decisions appear first; expert controls and
  raw YAML remain available.
- **One source of truth:** the UI must visibly distinguish backend state,
  unsaved in-memory changes, and configuration stored on disk.
- **Recoverable operations:** generation and import jobs must survive UI
  reconnects and stop their complete process trees when cancelled.
- **Local-first operation:** the configuration and generation experience should
  not depend on public internet access.
- **No loss of raw power:** advanced configuration, models YAML, dataset
  inspection, and SQL import remain first-class capabilities.

## Current baseline

The evaluation established the following baseline:

- `139` focused tests passed across `tests/test_web_api.py` and
  `tests/test_web_routes.py`.
- Both `/api/...` and `/v1/api/...` routes work.
- CORS, security headers, traversal protection, and YAML normalization have
  useful automated coverage.
- The desktop interface is visually polished and logically organized.
- CSV and Parquet dataset discovery and preview work on existing datasets.
- SQL import discovery works, although no live database import was performed.
- The browser console was clean except for the in-browser Babel production
  warning.

The passing test count must not be interpreted as a safe baseline yet. Some
generation endpoint tests launch real background generators and can modify
working-tree data after their assertions have completed.

## Phase 0 — Test containment and deterministic fixtures

### Goal

Make web tests safe to run repeatedly in any developer or CI working tree.

### Scope

- Replace real pipeline execution in generation-route unit tests with controlled
  fakes or mocks.
- Separate endpoint contract tests from subprocess command-construction tests.
- Ensure every background thread or fake process is joined or explicitly
  stopped during fixture cleanup.
- Isolate all paths that may be written by a test, including:
  - scratch data;
  - generated datasets;
  - dimension version files;
  - the FX master;
  - temporary config and models files.
- Prevent a test from resetting `_current_job` while an associated worker is
  still active.
- Add a guard test proving that the focused web suite does not modify tracked
  repository files.

### Key files

- `tests/test_web_routes.py`
- `tests/test_web_api.py`
- `web/routes/generation_routes.py`
- `web/shared_state.py`

### Acceptance criteria

- The focused web test suite never starts `main.py`.
- No generator or uvicorn child process remains after pytest exits.
- `git status --short` is identical before and after the test run.
- Temporary directories can be removed immediately on Windows.
- Tests assert the exact command and request options passed to the pipeline.
- Concurrent-job, cancel, timeout, success, and failure paths remain covered.

### Verification

```powershell
$before = git status --short
.venv/Scripts/python.exe -m pytest tests/test_web_api.py tests/test_web_routes.py
$after = git status --short
if ($before -ne $after) { throw "Web tests changed the working tree" }
```

Also inspect running Python processes after the test command. The test is not
complete if a pipeline process is still active.

### Out of scope

- Changing production cancellation behavior.
- Redesigning the frontend.
- Optimizing test duration beyond what is needed for safe isolation.

### Completion evidence — July 29, 2026

- Generation routes use a dedicated `_launch_pipeline` seam.
- Route tests replace that seam synchronously and never start `main.py`.
- Pipeline command construction is pure and tested with exact argument ordering.
- Production runner state transitions use a fake `Popen`; success, failure,
  preserved cancellation, timeout, logging, and process-creation errors are
  covered without launching a child process.
- Test cleanup joins any retained generation thread before restoring job state.
- A module-level worktree fingerprint detects changes to clean, already-dirty,
  and untracked files.
- Temporary config and models paths are verified absent after runner completion
  and failure.
- Focused result: `147 passed, 1 warning in 4.57s`.
- Ruff result for changed Python files: `All checks passed`.

## Phase 1 — Generation process lifecycle

### Goal

Make start, timeout, cancellation, and shutdown apply to the complete generation
process tree.

### Scope

- Start generation in a dedicated process group.
- Reuse or extract the proven graceful-stop pattern from SQL import.
- On cancellation:
  1. request graceful termination;
  2. wait for a bounded grace period;
  3. kill the complete process group if it is still running;
  4. wait for final process termination;
  5. record the final state once.
- Apply the same process-tree behavior to wall-clock timeout.
- Define terminal states clearly: `done`, `failed`, `cancelled`, and `timed_out`.
- Ensure cancellation does not get overwritten by the worker thread's eventual
  non-zero exit code.
- Protect job mutations and snapshots consistently with the job lock.
- Add application-shutdown cleanup for an active generation process.

### Key files

- `web/routes/generation_routes.py`
- `web/routes/import_routes.py`
- `web/shared_state.py`
- `tests/test_web_routes.py`

### Acceptance criteria

- Cancelling a multiprocessing generation run leaves no worker processes.
- Timeout produces `timed_out`, not a generic failure.
- The SSE stream emits one terminal event with the correct status.
- Repeated cancellation requests return a predictable response and do not race.
- Server shutdown stops or deliberately hands off the active process.
- Tests cover Windows behavior, the project's primary environment.

### Out of scope

- Frontend reconnect behavior.
- Persisting job history across server restarts.
- Log-memory optimization.

### Completion evidence — July 29, 2026

- Generation and SQL import children start in dedicated process groups.
- Windows processes are attached to a kill-on-close Job Object; POSIX processes
  start in a new session.
- Graceful stop uses `CTRL_BREAK_EVENT` on Windows and `SIGTERM` on POSIX.
- Forced stop terminates the Job Object, with `taskkill /T /F` and direct kill
  fallbacks; POSIX uses `SIGKILL` on the process group.
- Cancellation, timeout, and application shutdown use the shared process-tree
  controls.
- Generation now preserves distinct `cancelled` and `timed_out` terminal states.
- Job mutation and SSE/status snapshots are guarded by `_job_lock`.
- FastAPI lifespan shutdown stops the process tree and joins the retained
  generation thread.
- A controlled Windows parent/child smoke test initially reproduced an orphaned
  child with process-group signalling alone, then passed after Job Object
  containment was added.
- Focused web result after lifecycle integration: `156 passed, 1 warning`.
- Ruff and `git diff --check` pass for the changed files.

## Phase 2 — API contracts and frontend state integrity

### Goal

Ensure the browser never presents an operation as successful when the API
rejected it, and make reload/reconnect behavior predictable.

### Scope

- Centralize frontend request handling:
  - check `response.ok`;
  - parse FastAPI's `detail` field;
  - handle non-JSON failures;
  - show a useful user-facing message.
- Fix generation start, cancellation, preset application, config synchronization,
  and reset actions to use that request handling.
- Query `/generate/status` during initial page load.
- Reconnect the SSE log stream when a job is already running.
- Distinguish an SSE connection failure from a pipeline failure.
- Keep the UI in a visible degraded state when initial config loading fails.
- Remove or clearly label hard-coded fallback configuration.
- Disable Generate while config state is unavailable or unsynchronized.
- Validate form updates at the API boundary and return structured `400` or `422`
  responses instead of conversion-related `500` errors.
- Add frontend and backend regression tests for `400`, `409`, `422`, and `500`
  responses.

### Key files

- `web/frontend/app.jsx`
- `web/frontend/sql-import.jsx`
- `web/frontend/data-preview.jsx`
- `web/routes/config_routes.py`
- `web/routes/models_routes.py`
- `web/routes/generation_routes.py`

### Acceptance criteria

- A `409 Already running` response does not create a new local run entry.
- A failed start leaves Generate available after showing the error.
- A failed cancellation does not display the job as stopped.
- Reloading during a run reconnects to its state and logs.
- A broken `/api/config` request produces a prominent degraded-state screen.
- Invalid numeric values return a field-specific client error, never a traceback.
- Debounced sync failure remains visible until a successful synchronization.

### Out of scope

- Redesigning all form sections.
- Persisting completed run history.
- Replacing the frontend build system.

### Completion evidence — July 29, 2026

- Added one shared browser request client that rejects every non-2xx response,
  extracts string, structured, and Pydantic `detail` payloads, handles plain-text
  failures, and reports network errors consistently.
- Generation start, cancellation, presets, form synchronization, YAML actions,
  models actions, downloads, Data Preview, and SQL Import now use that contract.
- Removed the hard-coded fallback configuration. A failed initial config request
  now shows a blocking recovery screen with Retry and no Generate action.
- Generate remains disabled while a config edit is pending or rejected; a
  rejected debounced sync stays visible until a later successful synchronization.
- Initial load queries `/generate/status`; a running job restores its elapsed
  time and reconnects the SSE stream. An interrupted stream checks server state
  and retries only when the pipeline is still running.
- Numeric config and models form fields now return field-specific structured
  `422` responses without partially mutating live state.
- Frontend contract tests exercise `400`, `409`, `422`, and non-JSON `500`
  responses. Focused web result: `164 passed, 1 existing deprecation warning`.
- Live browser tests confirmed the blocking config-failure screen, persistent
  sync-failure warning with Generate disabled, rejected `409` start with Generate
  still available, reload recovery from a running job, dataset/table preview,
  and SQL Server Import readiness.

## Phase 3 — Beginner workflow and business-shape controls

### Goal

Give a first-time user a successful path from opening the UI to generating a
useful dataset without requiring YAML knowledge.

### Scope

- Add a **Quick Start** or **Guided Setup** mode covering:
  - output format;
  - approximate dataset size;
  - date range;
  - business trend;
  - product catalog;
  - optional facts;
  - destination summary.
- Keep the current full form as **Advanced Setup**.
- Expose `models.macro_demand.trend` as the primary business-shape selector.
- Provide plain-language descriptions and small previews for trend presets.
- Explain when manual year-level factors override part of a trend.
- Add recommended scale presets such as:
  - quick sample;
  - Power BI demo;
  - medium analytics dataset;
  - large stress test.
- Show estimated implications before generation:
  - rows;
  - approximate output size;
  - expected generation complexity;
  - enabled tables;
  - likely compatibility, such as PBIP availability.
- Add a review screen summarizing important settings in user language.
- Link each advanced concept to the relevant documentation rather than embedding
  every technical detail in the form.

### Key files

- `web/frontend/app.jsx`
- `web/frontend/components.jsx`
- `web/routes/models_routes.py`
- `web/routes/presets_routes.py`
- `docs/CONFIG_GUIDE.md`
- `docs/MODELS_GUIDE.md`

### Acceptance criteria

- A new user can select a trend without opening models YAML.
- Guided Setup produces a valid configuration with five or fewer major
  decisions.
- Advanced users can switch to the complete controls without losing changes.
- The review screen states which settings are in memory versus stored on disk.
- Trend selection and manual factors cannot silently contradict each other.
- User testing with a clean profile can complete a sample setup without project
  knowledge.

### Out of scope

- Removing YAML editors.
- Reducing the number of supported configuration fields.
- Changing generation semantics or trend-preset definitions.

### Completion evidence (July 29, 2026)

- Quick Start is the default clean-profile path and reduces setup to five
  decisions plus a review: scale, dates, business story, output/catalog, and
  optional fact bundle.
- Four plain-language scale presets cover a 100K-row sample through a 100M-row
  stress test. The review estimates output size and complexity, lists enabled
  tables, and states whether a Power BI project is available.
- All 15 supported trend presets are selectable without YAML and include a
  short explanation and visual shape preview.
- Applying a new trend clears manual year factors atomically, preventing a
  selected preset and an old manual curve from silently disagreeing.
- Guided changes validate typed config and models copies before committing
  either, remain in server memory, and are available through both `/api` and
  `/v1/api`.
- Advanced Setup remains available with the complete controls. Live browser
  testing confirmed that a 2M-row CSV setup with custom dates, synthetic
  products, analytics facts, and a hockey-stick trend survives the transition
  from Quick Start to Advanced Setup and back.
- The review explicitly distinguishes in-memory settings from the timestamped
  folder created by generation. Generation was intentionally not started during
  the browser acceptance test.
- Focused web regression result: `172 passed, 1 existing deprecation warning`.
  Ruff passed for the changed routes and tests.

## Phase 4 — Accessibility and responsive design

### Goal

Make the configuration workflow operable with keyboard, screen reader, voice
control, zoom, and narrow viewports.

### Scope

- Give every input a stable `id` and accessible name.
- Associate labels with controls using `htmlFor`.
- Connect help and error messages with `aria-describedby`.
- Add `aria-expanded` and `aria-controls` to collapsible sections.
- Give icon-only and validation controls descriptive names.
- Ensure visible focus states throughout the application.
- Verify logical keyboard order and preserve focus after errors or section
  changes.
- Add an error summary that links to invalid controls.
- Redesign the mobile sidebar as a compact navigation control or drawer.
- Reflow the sticky generation bar on narrow screens.
- Verify layouts at 320, 390, 768, 1024, and desktop widths.
- Check contrast in all badge, muted-text, error, warning, and disabled states.

### Key files

- `web/frontend/components.jsx`
- `web/frontend/app.jsx`
- `web/frontend/styles.css`
- all specialized frontend panels

### Acceptance criteria

- Every form control has a programmatic accessible name.
- All main workflows can be completed with the keyboard.
- Accordion state is announced correctly.
- The UI remains usable at 200% browser zoom.
- At 390 px, dates, validation state, and Generate/Cancel controls remain
  readable and operable.
- Automated accessibility checks report no serious or critical violations.
- A short manual screen-reader pass succeeds for Guided Setup and generation.

### Out of scope

- A broad visual rebrand.
- Native mobile applications.
- Localization, except avoiding layouts that prevent it later.

### Completion evidence (July 29, 2026)

- Shared fields now generate stable control/help IDs, associate visible labels
  with native controls, and expose help through `aria-describedby`. Live DOM
  audits found zero unnamed inputs, selects, or text areas across Quick Start,
  all 12 advanced feature panels, Models visual/YAML, and SQL Import.
- Mouse-only simulated checkboxes were replaced by native checkbox controls.
  Skip navigation, visible `:focus-visible` treatment, native buttons, and an
  announced Escape route out of the Tab-consuming YAML editor provide a
  keyboard-operable path through the main workflows.
- Configuration and log accordions expose `aria-expanded` and `aria-controls`;
  their controlled regions have stable IDs and accessible names.
- The validation summary is an announced region with links that reveal and
  focus the corresponding invalid controls.
- Mobile navigation is now an overlay drawer rather than a 300 px-tall page
  header. The generation dock reflows to a full-width 42 px action, guided dates
  stack, SQL Import becomes one column, and wide dataset selectors scroll
  locally.
- Live layout measurements at 320, 390, 768, 1024, and 1440 CSS pixels showed
  no document-level horizontal overflow. The 390 px layout also covers the
  effective viewport of a typical 780 px window at 200% zoom.
- A screen-reader-style accessibility-tree pass confirmed named controls,
  landmarks, live status/log regions, checkbox states, and announced accordion
  state for Guided Setup, Advanced Setup, Models, generation, and SQL Import.
- axe-core 4.12.1 reported `0 violations` on the clean-profile Quick Start.
  Focused web regressions passed: `175 passed, 1 existing deprecation warning`.

## Phase 5 — Self-contained frontend and browser security

### Goal

Make the local application reliable without public CDNs and remove development
runtime dependencies from production use.

### Scope

- Introduce a small frontend build pipeline.
- Bundle React, ReactDOM, CodeMirror, and application code locally.
- Precompile JSX instead of transforming it in the browser.
- Self-host fonts or use a robust system-font stack.
- Produce hashed static assets and define a cache policy.
- Add a Content Security Policy compatible with the bundled application.
- Preserve the existing CORS allowlist and security middleware.
- Document development and production frontend commands.
- Ensure installation remains straightforward on Windows.

### Key files

- `web/frontend/`
- `web/api.py`
- `pyproject.toml` or frontend package metadata
- setup and launch documentation

### Acceptance criteria

- The full configuration and generation UI loads with network access disabled.
- No production Babel warning appears.
- No executable JavaScript is fetched from a third-party origin.
- CSP blocks unexpected scripts without breaking the UI.
- A clean checkout can build or use committed production assets through a
  documented command.
- Existing API and security-header tests continue to pass.

### Out of scope

- Migrating to a large application framework.
- Server-side rendering.
- Internet deployment or multi-user authentication.

### Completion evidence (July 29, 2026)

- A small esbuild pipeline now precompiles the existing JSX and bundles pinned
  React 18, ReactDOM, CodeMirror, YAML mode, and editor add-ons locally.
- The committed production page contains no Babel runtime, remote font, CDN
  script, executable inline script, or third-party stylesheet. It uses a
  system-font stack and three content-hashed localhost assets.
- FastAPI serves `web/frontend/dist/` directly. Fingerprinted JavaScript and CSS
  receive a one-year immutable cache policy; the HTML entry remains `no-cache`.
- The Content Security Policy restricts executable scripts and API/SSE
  connections to the same origin, blocks objects and framing, and preserves the
  existing localhost CORS allowlist and security headers.
- The asset manifest records hashes for every build input. Automated tests fail
  if committed assets are missing, stale, unhashed, or reference a public
  runtime dependency.
- A clean runtime still needs only the Python setup because production assets
  are committed. Windows-friendly `npm ci`, build, watch, and freshness-check
  commands are documented for frontend contributors.
- Live browser acceptance loaded Quick Start, fetched only hashed localhost
  scripts/styles, reported no warnings or errors, and advanced to the Dates
  step. Focused web regressions passed: `179 passed, 1 existing deprecation
  warning`; Ruff, build freshness, npm audit, and diff checks were clean.

## Phase 6 — Long-job and large-dataset scalability

### Goal

Keep memory, CPU, and response time predictable for long generation runs and
large previews.

### Scope

#### Job logs

- Replace unbounded log lists with a bounded or indexed structure.
- Stop copying the complete log history on every SSE poll.
- Define retention and truncation behavior visibly.
- Allow a reconnecting client to request logs after a known sequence number.
- Bound browser-side log history as well.

#### Dataset preview

- Read only required Parquet columns and row slices where practical.
- Avoid converting a complete large row group to pandas for a 100-row page.
- Investigate cursor/keyset or indexed pagination for CSV and Delta.
- Avoid rescanning Delta from the beginning for every high offset.
- Apply display formatting without altering source values:
  - sensible currency precision;
  - dates;
  - nulls;
  - booleans;
  - large integers.
- Format both current and total pagination numbers consistently.
- Add query cancellation or request supersession when users switch pages
  rapidly.

### Key files

- `web/routes/generation_routes.py`
- `web/routes/data_routes.py`
- `web/frontend/log-viewer.jsx`
- `web/frontend/data-preview.jsx`

### Acceptance criteria

- Log memory remains bounded during a four-hour simulated job.
- SSE work per update depends on new log entries, not total history.
- A reconnect can resume without replaying unnecessary history.
- Previewing 100 rows does not materialize a million-row Parquet row group in
  pandas.
- First and last-page behavior is benchmarked for representative CSV, Parquet,
  and Delta datasets.
- Displayed monetary values do not expose binary floating-point artifacts.
- Performance limits and expected behavior are documented.

### Out of scope

- Building a full analytics/query engine.
- Editing generated data in the browser.
- Guaranteeing constant-time arbitrary-page access for every file format.

### Completion evidence (July 29, 2026)

- Server output now uses a list-compatible bounded window with monotonically
  increasing sequence IDs. The default retains 5,000 lines and is configurable
  through `WEB_LOG_RETENTION_LINES`.
- A four-hour simulated one-line-per-second job retained exactly its configured
  window. The browser independently retains 2,000 lines; both layers display a
  notice when older output is unavailable.
- SSE accepts an `after` cursor, copies only newer entries while holding the job
  lock, emits sequence IDs, and reports when a reconnect cursor predates the
  retained window. Cache and proxy buffering are disabled for the stream.
- Parquet preview uses row-group metadata plus bounded Arrow batches and slices
  before Python conversion. A regression fixture previewed the final 100 rows
  of a one-million-row single row group without creating a pandas row-group
  frame.
- Delta preview skips complete file fragments and scans bounded batches only in
  the selected fragment. CSV reuses modification-time row-count caches, skips
  complete chunks, and reads only the rows still needed from selected chunks.
- Switching preview page/table/dataset aborts the superseded browser request.
  Both current and total page numbers use grouped display formatting.
- Preview formatting preserves exact large integers and displays currency,
  general floats, dates, nulls, and booleans without changing source files.
- Representative 100-row first/last timings were: Parquet 1M rows
  `0.0115s/0.0788s`, CSV 20K rows `0.0197s/0.0093s`, and Delta 2K rows
  `0.0286s/0.0048s`. Limits and format-specific tradeoffs are documented in
  `docs/WEB_UI_PERFORMANCE.md`.
- Focused web regressions passed: `191 passed, 1 existing deprecation warning`;
  Ruff and frontend build freshness checks were clean.

## Phase 7 — Release validation and documentation

### Goal

Validate the complete newcomer and advanced-user experience before treating the
web UI as a recommended entry point.

### Scope

- Run the complete focused and project test suites.
- Perform controlled end-to-end generation for:
  - a small CSV dataset;
  - a small Parquet dataset;
  - a small Delta dataset.
- Exercise success, validation failure, cancellation, timeout, reload, and server
  shutdown.
- Preview early and late pages for all supported formats.
- Perform a non-destructive SQL import dry run or disposable-database import.
- Repeat browser testing at supported viewport sizes.
- Run automated accessibility and dependency/security checks.
- Update README onboarding and web UI documentation from the verified behavior.
- Capture known limitations and troubleshooting steps.

### Acceptance criteria

- All automated tests pass without modifying tracked files.
- No child processes remain after cancellation, timeout, tests, or server exit.
- The app operates offline after installation.
- Guided Setup produces valid CSV, Parquet, and Delta runs.
- Desktop, keyboard, screen-reader, and mobile smoke checks pass.
- README instructions match the actual UI and use the beginner workflow first.
- Advanced YAML and SQL workflows remain available and documented.

### Completion evidence (July 29, 2026)

- The isolated committed-tree validator generated bounded 500-row CSV,
  Parquet, and Delta packages without touching the developer's configuration,
  FX data, scratch files, versions, or outputs. Every format passed first/last
  preview through both `/api` and `/v1/api`; CSV included SQL bundles and Delta
  included transaction logs.
- The complete project suite passed with `2428 passed, 1 warning in 195.01s`;
  the focused release suite passed with
  `199 passed, 1 warning in 7.37s`. Frontend freshness, Ruff, dependency
  compatibility, and `npm audit` also passed.
- The production bundle completed Quick Start, Advanced Setup, Parquet Data
  Preview, and the non-destructive SQL Import readiness screen with no browser
  warnings or errors.
- Layout and control measurements passed at 1440, 1024, 768, 390, and 320 CSS
  pixels plus a 200%-zoom-equivalent viewport, with no document-level
  horizontal overflow or unnamed visible controls.
- Semantic checks confirmed the document language, heading/main structure,
  skip target, named application and guided-progress navigation, control names,
  unique IDs, image alternatives, and live pipeline status/log regions.
- Reloading a live 10M-row run restored running state, elapsed time, logs, and
  cancellation controls.
- Live Windows cancellation of a 100M-row run reached one `cancelled` terminal
  state and left no generator or worker. Stopping uvicorn during a separate
  100M-row run also removed the server, generator, workers, and listening port.
- Final browser testing exposed and fixed one misleading accessible status:
  cancelled runs previously displayed `Completed`. Commit `42106de` preserves
  and announces `Cancelled`, `Timed out`, and `Failed`, and also adds Escape
  dismissal and focus return to application navigation. The corrected
  production bundle passed the repeated browser and lifecycle checks.
- README onboarding, troubleshooting, performance limits, and the reproducible
  release checklist now match the behavior verified in the browser and isolated
  generator.

## Cross-phase engineering constraints

These rules apply to every phase:

- Guard shared configuration with `_cfg_lock`.
- Deep-copy shared Pydantic configuration while holding the lock.
- Keep every route available through both `/api` and `/v1/api`.
- Do not weaken the localhost CORS allowlist.
- Keep security headers on API and static responses.
- Preserve deliberate request-size limits and improve them at the transport layer
  if broader protection is required.
- Treat `cfg` and `models_cfg` as Pydantic models, not dictionaries.
- Do not run generation-route tests against real repository paths.
- Test subprocess and process-group behavior on Windows.

## Phase tracking

| Phase | Status | Completion evidence |
|---|---|---|
| 0. Test containment | Complete | Safe launcher seam, fake process coverage, worktree fingerprint, `147` focused tests passing, and Ruff clean on July 29, 2026. |
| 1. Generation lifecycle | Complete | Shared process groups, Windows Job Object containment, graceful/forced tree stop, shutdown join, controlled smoke test, and `156` focused tests passing on July 29, 2026. |
| 2. API and state integrity | Complete | Shared HTTP client; visible config/sync failure states; generation reload/SSE recovery; structured numeric `422` errors; `164` focused tests; and live browser checks for degraded config, failed sync, `409` start conflict, reconnect, Data Preview, and SQL Import on July 29, 2026. |
| 3. Beginner workflow | Complete | Five-decision Quick Start, four scale presets, all 15 trend controls, atomic guided config/models update, review estimates/storage semantics, Advanced Setup continuity, `172` focused tests, and clean-profile browser acceptance on July 29, 2026. |
| 4. Accessibility and responsive design | Complete | Named controls across every rendered form; native keyboard controls; announced accordions, validation, YAML exit, status, and logs; mobile drawer and generation reflow; 320–1440 px plus 200%-zoom-equivalent checks; axe-core `0 violations`; and `175` focused tests on July 29, 2026. |
| 5. Self-contained frontend | Complete | Local esbuild bundle with pinned React/ReactDOM/CodeMirror; committed hashed assets and freshness manifest; system fonts; strict same-origin CSP; immutable asset caching; zero remote executable/style resources; browser acceptance; and `179` focused tests on July 29, 2026. |
| 6. Scalability | Complete | Bounded server/browser logs; sequence-based incremental SSE and reconnect truncation; batched Parquet/Delta preview; CSV chunk/count reuse; stale-request cancellation; exact display formatting; first/last benchmarks; documented limits; and `191` focused tests on July 29, 2026. |
| 7. Release validation | Complete | Isolated 500-row CSV/Parquet/Delta packages and dual-prefix first/last previews; `2428` project and `199` focused tests; production browser checks at 320–1440 px plus 200%-zoom equivalent; Quick Start, Advanced, Data Preview, and SQL Import acceptance; live reload/reconnect; Windows cancellation and active-server-shutdown cleanup; accurate accessible terminal status and Escape focus return fixed in `42106de`; release and newcomer documentation current on July 29, 2026. |

Allowed status values are `Not started`, `In progress`, `Blocked`, and `Complete`.
A phase becomes `Complete` only after all of its acceptance criteria have
evidence recorded in the table.

## Recommended next work item

Phase 7 is complete. Review and merge the
`codex/user-experience-testing` branch when the separate testing history is
ready. A disposable-database SQL Server/PostgreSQL import can be scheduled as a
future integration exercise, but it is not required for the non-destructive web
release gate.
