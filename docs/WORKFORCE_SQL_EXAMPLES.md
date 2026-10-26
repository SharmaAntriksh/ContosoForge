# Workforce SQL examples

These queries replace the optional `EmployeeAssignmentHistory`,
`EffectiveSalespersonAssignment`, and `StoreSalesActuals` views. They are not
installed by the default SQL bundle. Base tables and the unified `vw_Sales`
already provide their inputs; choose the dates and grouping for the question.

Examples use the default PascalCase identifiers. In SQL Server, use
`SET QUOTED_IDENTIFIER ON` and replace `TRUE` with `1`. Qualify tables/views with
your schema (`dbo` on SQL Server, `public` on PostgreSQL by default); a custom
view schema drops the `vw_` prefix. Snake imports require snake_case identifiers.
These examples do not create or modify database objects.

## Employee assignment history

One row per assignment segment. Employee attributes describe the original home
organization; store attributes describe the segment's work location. No current
flag filter belongs in a history query. Counting these rows counts placements,
including transfers, rather than distinct people.

```sql
SELECT a."AssignmentKey", a."EmployeeKey", e."EmployeeName",
       e."HomeStoreKey", a."StoreKey", s."StoreName", s."StoreRegion",
       a."StartDate", a."EndDate", a."RoleAtStore", a."FTE",
       a."IsCurrentAssignment", a."AssignmentStatus", a."TransferReason"
FROM "EmployeeStoreAssignments" AS a
JOIN "Employees" AS e ON e."EmployeeKey" = a."EmployeeKey"
JOIN "Stores" AS s ON s."StoreKey" = a."StoreKey";
```

## Effective salesperson assignment on a date

This query answers where eligible salespeople worked on June 10, 2024. Replace
the date literals for another day, and replace `Sales Associate` if the primary
sales role is configured differently. Employment and assignment endpoints are
inclusive; closure and renovation endpoints follow the operating-date contract.
Dataset-end status flags do not exclude valid historical work.

```sql
SELECT a."EmployeeKey", e."EmployeeName", a."StoreKey", s."StoreName",
       a."RoleAtStore", a."FTE", a."StartDate", a."EndDate"
FROM "EmployeeStoreAssignments" AS a
JOIN "Employees" AS e ON e."EmployeeKey" = a."EmployeeKey"
JOIN "Stores" AS s ON s."StoreKey" = a."StoreKey"
WHERE '2024-06-10' BETWEEN a."StartDate" AND a."EndDate"
  AND '2024-06-10' >= e."HireDate"
  AND (e."TerminationDate" IS NULL OR '2024-06-10' <= e."TerminationDate")
  AND e."IsSalesperson" = TRUE
  AND a."RoleAtStore" = e."Title"
  AND ((s."StoreType" = 'Online' AND a."RoleAtStore" = 'Online Sales Representative')
       OR (s."StoreType" <> 'Online' AND a."RoleAtStore" = 'Sales Associate'))
  AND '2024-06-10' >= s."OpeningDate"
  AND (s."ClosingDate" IS NULL OR '2024-06-10' < s."ClosingDate")
  AND (s."RenovationStartDate" IS NULL OR s."RenovationEndDate" IS NULL
       OR '2024-06-10' < s."RenovationStartDate"
       OR '2024-06-10' >= s."RenovationEndDate");
```

For sale-level attribution, use the transaction's `OrderDate` instead of the
literal and match both `EmployeeKey` and `StoreKey`. Joining intervals to sales
without that date predicate can multiply lines. The packaged quality report
checks attribution independently.

## Store sales actuals

One row per store and currency over the entire emitted sales period. The inner
aggregation makes each order contribute once to the average, regardless of its
number of lines. `NetSalesAmount` is `NetPrice * Quantity`, after discounts and
before returns; the former `GrossSalesAmount` label was incorrect. Returns are
separate events and are not subtracted here. Do not sum different currencies.

```sql
SELECT o."StoreKey", o."CurrencyKey", COUNT(*) AS "OrderCount",
       SUM(o."TransactionAmount") AS "NetSalesAmount",
       AVG(o."TransactionAmount") AS "AvgTransactionValue"
FROM (
    SELECT "StoreKey", "CurrencyKey", "OrderNumber",
           SUM(CAST("NetPrice" AS DECIMAL(19, 4)) * "Quantity") AS "TransactionAmount"
    FROM "vw_Sales"
    GROUP BY "StoreKey", "CurrencyKey", "OrderNumber"
) AS o
GROUP BY o."StoreKey", o."CurrencyKey";
```

`vw_Sales` already resolves flat versus split order output. Order counts and
average order values require `OrderNumber`: use `sales_order` output or flat
sales with order columns enabled. For flat output that omits order columns,
only line revenue (`SUM(NetPrice * Quantity)`) is available. To limit a reporting
period, filter `OrderDate` inside the inner query before aggregating orders.

## Default workforce views

| View | Grain and use |
|---|---|
| `vw_CurrentEmployeesByStore` | One latest active placement per assigned employee at dataset end, including temporary placements. Employee home hierarchy remains separate. |
| `vw_CurrentStoreManager` | The latest active home-store manager placement, identified by employee key and matching manager title/role. Visiting managers are excluded. If the home manager is temporarily elsewhere, the home store has no row; the view does not appoint a replacement. |
| `vw_StoreStaffingActualVsPlan` | One row per store, including stores with no current staff. Planned headcount is compared with current active placement headcount and summed assignment FTE. Closed-store plans remain populated; a negative variance there does not establish a vacancy. |

Here, current means `defaults.dates.end`, not today's date. These views select
the latest active segment under the generator's timeline contract; they do not
accept an arbitrary as-of date. `CurrentStoreManager` is safe as a store lookup
only when the generated one-home-manager contract holds. Duplicate home managers
in imported or older data are a data-quality problem, not a reason to select one
arbitrarily. Staff reporting lines still come from the original employee
hierarchy, including when their manager is temporarily away.

Regenerating or re-emitting SQL changes future bundles. Applying a new bundle
does not drop the three older optional views from an existing database. Review
external consumers before manually removing those objects; this change does not
execute a migration against existing databases.
