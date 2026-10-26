# Workforce review: final verification

Verification date: 2026-10-26. This records the working-tree result after the
five review/remediation phases, not a certification of a released artifact.
The review began with `4dfd14f` and followed the later implementation; older
model involvement and commit authorship were not evidence of defects or user
authorization. The home-organization and manager policies below were explicitly
selected by the user during remediation.

## Business model and decisions

The model supports employee identity and original reporting organization in
`Employees`, operational store plans/lifecycle in `Stores`, effective primary
placements in `EmployeeStoreAssignments`, and transaction attribution in
`Sales` or `OrderHeader`. Keep those grains separate. A bridge segment is not
another employee, a staffing plan is not actual headcount/FTE, and a home store
does not determine the store of a historical sale.

`HomeStoreKey` and the employee reporting hierarchy retain the original generated
organization. The bridge records permanent transfers and temporary placements.
That is a chosen contract, not an unresolved synchronization defect. It does
not model changing supervisory authority at a temporary destination.

Confirmed issues addressed in earlier phases were opaque-key decoding in SQL
diagnostics, incomplete transaction eligibility verification, unsafe employment
extensions during optional coverage repair, manager retention into already
managed destinations after closure, missing planned headcount at the transfer
runner boundary, and the incorrect gross label for discounted sales revenue.
See the [analytical contract](ANALYTICAL_SEMANTICS.md#stores-and-workforce) and
the tests linked below for the resulting behavior. No new business-correctness
failure was observed in the Phase 5 scenario.

## View disposition

| Reviewed view | Final disposition and reason |
|---|---|
| `EmployeeAssignmentHistory` | Documentation query. A descriptive join over base tables adds convenience but no new grain or business calculation. |
| `CurrentEmployeesByStore` | Keep in the default bundle. It supplies latest active placements at dataset end; it includes visitors and does not rewrite home hierarchy. |
| `EffectiveSalespersonAssignment` | Documentation query. The example resolves a specific date and checks employment, role/channel, and store operation. An interval projection alone does not establish sale eligibility. |
| `CurrentStoreManager` | Revise and keep. Require matching home store and manager title/assignment role. Visiting managers are excluded; an absent home manager is not replaced implicitly. |
| `StoreStaffingActualVsPlan` | Keep. The left join preserves unstaffed stores and separates planned headcount, actual headcount, and FTE. Closed-store plans remain populated and do not establish vacancies. |
| `StoreSalesActuals` | Documentation query using `NetSalesAmount`. Aggregate lines to orders before calculating average order value, and keep currencies separate. No repository application consumer required an installed aggregate view. |

The base-table projections remain available. Removing the optional views from
future bundles does not drop objects in existing databases. Examples and usage
limits are in [WORKFORCE_SQL_EXAMPLES.md](WORKFORCE_SQL_EXAMPLES.md).

## Observed generation evidence

The independent acceptance run uses seed 2468, dates 2022-01-01 through
2023-12-31, 40 stores, 1,000 configured customers, and 6,000 requested sales
lines. Dimensions, facts, packaging output, configuration copies, version files,
and an FX-master copy are isolated in pytest temporary directories. It invokes
the repository venv interpreter directly.

Observed output:

- 40 stores, 527 employee identities, 1,149 assignment segments.
- 4 stores opening after the sales window starts, 13 closures, and 12 stores
  with renovation dates, including closure-related renovation periods.
- 180 renovation reassignment segments, 14 renovation returns, 124 closure
  reassignment segments, 98 backfills, 234 employee-request transfers, and
  3 new-store-opening transfers.
- Flat output: 6,000 sales lines. Split output: 3,334 order headers, each
  checked as an attribution source rather than counted as sales lines.

The [acceptance oracle](../tests/test_workforce_acceptance.py) independently
enumerates each operating store-day and checks for an employed, eligible
salesperson. Every emitted employee/store/date tuple in flat sales and split
order headers matches exactly one valid assignment, employment window, eligible
role, and operating store. It also verifies identity references, contiguous
exclusive primary placements, closure manager terminations, original hierarchy,
dataset-end headcount/FTE reconciliation, and unchanged employee data after
sales generation. The oracle does not call production workforce lookup,
coverage, or quality-report helpers.

The final focused run passed **225 tests** spanning this acceptance run,
diagnostics, repair, transfers, published workforce contracts, example query
execution, SQL rendering, and re-emission. Ruff and `git diff --check` passed.
Existing snapshots and datasets were not regenerated. Worker/chunk invariance
and golden sales fingerprints also passed during Phase 3; those checks are
reproducibility evidence, not independent business validation.

## What remains unverified

1. Live SQL Server and PostgreSQL compilation/deployment were not executed.
   SQLite checks relational results after limited dialect adaptation; renderer
   and fake-import tests cannot prove live-server success.
2. Power BI relationships/visibility are covered as published metadata. No
   Power BI engine evaluated interval-aware measures or report interactions.
3. The generated acceptance scenario covers one seed, Parquet packaging,
   standard sales roles, and separate flat/split modes. Negative fixtures cover
   custom role eligibility, CSV diagnostics, date boundaries, interior gaps,
   and both attribution sources present together; they do not establish a full
   generation matrix over those alternatives.
4. Coverage diagnostics that infer observation bounds from assignment dates
   cannot detect missing periods outside those bounds. The independent oracle
   uses the explicitly configured sales window, avoiding that blind spot for
   this run.

These are limits of the evidence, not newly confirmed generator defects. The
[test guide](../tests/README.md#sales-determinism-and-stored-snapshots) separates
business oracles, diagnostic mutation cases, contract assertions, and snapshots.

Before claiming live database/report integration, run the generated SQL in
fresh SQL Server and PostgreSQL test databases, then evaluate the intended
historical staffing/sales measures in Power BI. Existing datasets need an
explicit regeneration/import decision before they gain the generator fixes;
this review did not overwrite them or migrate any database.
