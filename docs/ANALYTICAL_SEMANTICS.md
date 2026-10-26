# Analytical measure and event semantics

This document defines the generator contracts that matter when aggregating the
model or validating a generated dataset.

## Monetary fields

- `Sales.UnitPrice`, `Sales.UnitCost`, and `Sales.NetPrice` are **per-unit**
  values. Sales line revenue is `NetPrice * Quantity`; line cost is
  `UnitCost * Quantity`.
- `Sales.DiscountAmount` is a per-unit discount and satisfies
  `UnitPrice - DiscountAmount = NetPrice` after currency rounding.
- `Returns.ReturnNetPrice` retains its compatibility name but is a **summable
  return-event amount**: `Sales.NetPrice * ReturnQuantity`. Sum the field
  directly; do not multiply it by `ReturnQuantity` again.

## Dates and customer lifecycle

- Sales eligibility is exact to the day and inclusive:
  `CustomerStartDate <= OrderDate <= CustomerEndDate` when an end date exists.
- Sales and subscriptions stop at `defaults.dates.end`.
- `dates.post_sale_tail_days` extends the Date dimension only, allowing genuine
  post-sale returns, complaints, and resolutions to remain date-conformed. It
  does not create extra sales demand months.
- The quality report verifies that return, complaint, and resolution dates all
  fall inside the generated Date dimension.

## Promotions

- `Promotions.DiscountPct` is the **nominal target depth** associated with the
  selected promotion identity.
- Realized depth is `Sales.DiscountAmount / Sales.UnitPrice`. It can be lower
  than nominal when minimum-net-price or margin constraints cap the discount.
- Date, product/category, channel, loyalty, and exact new-customer-window rules
  are eligibility constraints; promotion salience affects selection among
  eligible promotions only.

## Wishlists

- Wishlist participation is sampled independently of later sales outcomes;
  future purchasers and high-frequency buyers receive no selection preference.
- The reported conversion definition is a purchase of the same
  customer/product pair on or after `AddedDate`.
- Converted wishlist rows are anchored to a stable purchase date; the configured
  `conversion_rate` targets this dated definition, not lifetime pair overlap.
- `pre_browse_days` permits browsing before acquisition within the documented
  lookback window.

## Complaints

- Product and fulfillment complaints are dated on or after `DeliveryDate`.
  Billing complaints are dated on or after `OrderDate`.
- When the relevant causal event falls outside the customer's observable
  lifecycle, the complaint remains a general-service event rather than being
  assigned an impossible order-line link.

## Subscriptions

- A trial is labeled `TrialNonConversion` only after the complete configured
  trial window has been observed. Trials cut off by the dataset or customer
  lifecycle end are right-censored and retain the corresponding boundary
  termination reason.

## Inventory

- `StockoutFlag = true` means demand exceeded available stock during the
  snapshot period.
- `DaysOutOfStock` estimates the resulting whole days without stock, rounded up
  and capped at 30 for monthly grain (90 after quarterly aggregation).
- A stockout always has at least one out-of-stock day; a non-stockout has zero.

## Stores and workforce

### Grain and analytical roles

| Table | Grain and identity | Analytical purpose |
|---|---|---|
| `Stores` | One physical or online store per `StoreKey`. | Store attributes, organizational rollups, operating dates, and planned roster size. |
| `Employees` | One person per opaque `EmployeeKey`; nullable `ParentEmployeeKey` references another employee. | Employee identity, employment lifecycle, and the generated home organization. It is not an effective-dated organization history. |
| `EmployeeStoreAssignments` | One primary assignment segment per `AssignmentKey`; `AssignmentSequence` orders segments within an employee. `EmployeeKey` and `StoreKey` reference their respective dimensions. | Effective store, role, and FTE. A transfer creates another segment, not another employee. |
| `Sales` | One order line; `(OrderNumber, OrderLineNumber)` identifies it when order columns are published. | Transaction measures and attribution to the employee and store on `OrderDate`. In split output, `OrderHeader` holds order-level attribution and `OrderDetail` holds lines. |

The assignment bridge is a sequence of primary placements, not a model of
simultaneous allocations across stores. Count distinct employees and sum segment
FTE **after** selecting a date; counting all history rows counts transfers too.
Employee keys do not encode role, hierarchy level, or store identity.

### Location, hierarchy, and current flags

- `Stores.StoreDistrictId` and `Stores.StoreRegionId` are the authoritative
  organizational identities. `StoreDistrict` and `StoreRegion` are display
  labels derived from those integer IDs. A district and region never cross a
  country-within-zone boundary.
- `Employees.HomeStoreKey` is the employee's original generated home store.
  Permanent transfers, temporary assignments, and closure reassignments do not
  update it. `ParentEmployeeKey`, `RegionId`, `DistrictId`, and `GeographyKey`
  likewise retain the original home organization. A retained employee can
  therefore have a closed home store while working at an open destination.
  This is the retained home-organization contract, not a dataset-end work
  location or an effective reporting hierarchy. Use
  `EmployeeStoreAssignments.StoreKey` with the inclusive condition
  `StartDate <= AsOfDate <= EndDate` for point-in-time location, role, and FTE.
- An assigned employee has exactly one `IsCurrentAssignment = true` segment.
  This flag identifies the **latest segment in the generated history**, including
  a terminated employee's final `Completed` segment. It does not by itself mean
  that the employee is still working. For dataset-end staffing, the current SQL
  views additionally require `AssignmentStatus = 'Active'`.
  Consecutive inclusive assignment segments are contiguous: the next segment
  starts one day after the previous segment ends.
- Corporate employees have null region, district, home-store, and geography
  keys. Regional managers carry only a region; district managers carry region
  and district; store-level employees carry the full home-store hierarchy.
- `Stores.EmployeeCount` remains the planned roster size for compatibility.
  Actual current headcount and FTE come from current active assignment rows and
  are published separately by `vw_StoreStaffingActualVsPlan`.
  The plan includes the physical store manager; online stores have a planned
  roster of one online sales representative. It is a headcount plan, not an FTE
  plan. A part-time employee contributes one to headcount and their assignment
  FTE to FTE. Do not sum `Employees.FTE` over a join to the entire bridge.
  Plans are not effective-dated and remain populated after closure; a closed
  store's negative headcount variance is not evidence of an unfilled vacancy.
- Store transaction actuals are fact-derived using the
  [store sales example](WORKFORCE_SQL_EXAMPLES.md#store-sales-actuals).
  Warehouse `TargetInventoryTurnover` and `TargetShrinkageRatePct` are targets;
  `InventorySnapshot.ShrinkageQuantity` is the observed generated outcome.

### Event dates and historical attribution

Dataset end means `defaults.dates.end`, not today's date, `dates.as_of_date`, or
the end of the Date dimension's post-sale tail. Historical placement uses
`StartDate <= AsOfDate <= EndDate` without filtering on current/status flags.
Employment includes both `HireDate` and `TerminationDate` when present. A store
operates from `OpeningDate` inclusive until `ClosingDate` exclusive; renovation
excludes `[RenovationStartDate, RenovationEndDate)`.

For example, a transfer on June 10 ends the source segment on June 9 and starts
the destination segment on June 10. A renovation reassignment uses the same
split, then returns the employee to the original store when renovation ends.
Closure ends the source placement on the day before closure: terminated staff
end employment that day, while retained staff move to another operating store
on the closing date. Historical sales at the source remain attributed there.

For every sale, resolve the employee/store pair on `OrderDate`, then check the
employee's employment dates, eligible sales role, and the store's operating
dates. A matching foreign key or an overlapping assignment alone is insufficient.
Store Managers do not qualify as salespeople. The online representative role
belongs to online stores; the configured primary sales role belongs to physical
stores. Employee and store dataset-end status flags cannot answer this
historical question.

### Manager identity and implementation limits

`Stores.StoreManager` and its first/last-name fields supply the original manager's
display name. They are not identity keys or a dated management relationship.
Use `EmployeeKey` for identity and `RoleAtStore = 'Store Manager'` in the effective
bridge for store management on a date. `ParentEmployeeKey` describes the generated
home reporting hierarchy; the bridge does not provide effective-dated reporting
lines or manager succession.

Store Managers are excluded from closure retention and terminate on the day
before closure. The retention target is still calculated from the full roster,
capped by the number of eligible non-manager employees. This prevents closure
reassignments from adding a second manager to an already managed destination.
Previously generated datasets can still contain that conflict and need
regeneration; joining multiple manager rows to sales multiplies transaction
amounts. Do not pick an arbitrary manager to hide such a conflict.

Optional `sales.coverage_policy: repair` extends existing sales assignment
segments only when the whole extension fits within the employee's inclusive
employment dates and overlaps none of their other assignments, including
non-sales roles. It never extends employment itself. Gaps without a safe
candidate remain unrepaired and are reported; missing employee data prevents
repair. Planned `Stores.EmployeeCount` is passed into transfer classification
so a destination below plan can produce the `Backfill` reason.

Generated SQL publishes `vw_CurrentEmployeesByStore`, `vw_CurrentStoreManager`,
and `vw_StoreStaffingActualVsPlan` for both SQL Server
and PostgreSQL. The PascalCase and snake_case import bundles are rendered from
the same view roster. CSV and Parquet Power BI models relate the assignment
bridge to Employee and Store so home-store and effective-store analysis remain
distinct.
These relationships do not automatically apply an assignment-date range to
sales. A historical employee/store analysis must supply that interval predicate
in SQL or an explicit measure/filter in Power BI.
`CurrentStoreManager` requires the placement store to match `HomeStoreKey` and
both employee title and assignment role to be Store Manager. Visiting managers
are excluded. A home manager temporarily working elsewhere is absent from this
view; no replacement or reporting-line change is inferred.

Assignment history, effective salesperson lookups, and store transaction
aggregates are [documentation examples](WORKFORCE_SQL_EXAMPLES.md), rather than
default SQL objects. The examples explain date predicates, currency grouping,
and order-level averages. `NetPrice * Quantity` is net of discounts, before
returns, and is labeled `NetSalesAmount`, replacing the incorrect gross label.

The packaged quality report enforces the assignment timeline and salesperson
as-of joins, workforce hierarchy/location contracts, country-bounded store
rollups, management/FTE/department policies, employee lifecycle consistency,
store closure causality, and the associated primary and foreign keys.

Sales attribution checks cover both `Sales` and `OrderHeader` when present.
Each transaction must match an eligible assignment role, the employee's inclusive
employment dates, and the store's operating dates, excluding renovation windows.
Dataset-end employee and assignment status flags do not disqualify historical
transactions. Missing eligibility columns are reported as verification failures.
The SQL and standalone Python coverage diagnostics check the union of eligible
assignment intervals over the observed assignment date range, including partial
months and online stores; they cannot detect a missing period outside that range.

## Currency and geography

- Geography `ISOCode` contains the local three-letter currency code used to join
  Geography, Currency, Stores, Sales, and Exchange Rates.
- Missing currency joins are fatal. The generator never silently substitutes
  USD or the first currency in the dimension.
- Norway, Poland, Sweden, and Switzerland use NOK, PLN, SEK, and CHF respectively.
