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

- `Stores.StoreDistrictId` and `Stores.StoreRegionId` are the authoritative
  organizational identities. `StoreDistrict` and `StoreRegion` are display
  labels derived from those integer IDs. A district and region never cross a
  country-within-zone boundary.
- `Employees.HomeStoreKey` is the employee's dataset-end home-location
  snapshot. It is not a historical join key. Use
  `EmployeeStoreAssignments.StoreKey` with the inclusive condition
  `StartDate <= AsOfDate <= EndDate` for point-in-time location, role, and FTE.
- An assigned employee has exactly one `IsCurrentAssignment = true` segment.
  Consecutive inclusive assignment segments are contiguous: the next segment
  starts one day after the previous segment ends.
- Corporate employees have null region, district, home-store, and geography
  keys. Regional managers carry only a region; district managers carry region
  and district; store-level employees carry the full home-store hierarchy.
- `Stores.EmployeeCount` remains the planned roster size for compatibility.
  Actual current headcount and FTE come from current active assignment rows and
  are published separately by `vw_StoreStaffingActualVsPlan`.
- Store transaction actuals are fact-derived in `vw_StoreSalesActuals`.
  Warehouse `TargetInventoryTurnover` and `TargetShrinkageRatePct` are targets;
  `InventorySnapshot.ShrinkageQuantity` is the observed generated outcome.

Generated SQL publishes `vw_EmployeeAssignmentHistory`,
`vw_CurrentEmployeesByStore`, `vw_EffectiveSalespersonAssignment`,
`vw_CurrentStoreManager`, and `vw_StoreStaffingActualVsPlan` for both SQL Server
and PostgreSQL. The PascalCase and snake_case import bundles are rendered from
the same view roster. CSV and Parquet Power BI models relate the assignment
bridge to Employee and Store so home-store and effective-store analysis remain
distinct.

The packaged quality report enforces the assignment timeline and salesperson
as-of joins, workforce hierarchy/location contracts, country-bounded store
rollups, management/FTE/department policies, employee lifecycle consistency,
store closure causality, and the associated primary and foreign keys.

## Currency and geography

- Geography `ISOCode` contains the local three-letter currency code used to join
  Geography, Currency, Stores, Sales, and Exchange Rates.
- Missing currency joins are fatal. The generator never silently substitutes
  USD or the first currency in the dimension.
- Norway, Poland, Sweden, and Switzerland use NOK, PLN, SEK, and CHF respectively.
