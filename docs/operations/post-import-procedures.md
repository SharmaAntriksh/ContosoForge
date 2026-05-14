# Post-Import Stored Procedures

The SQL Server import generates stored procedures in the `admin` and `verify` schemas for ongoing management and data validation. These run inside the imported database.

---

## Admin procedures

For managing indexes and constraints after the initial import.

| Procedure | Description |
|---|---|
| `admin.ManageColumnstoreIndexes` | Create, drop, or rebuild clustered columnstore indexes on fact tables |
| `admin.ManagePrimaryKeys` | Backup, drop, restore, or recreate PK/FK constraints (enables CCI swap and matches the importer's `-DropPKBeforeLoad` / `-RestorePKAfterLoad` behavior) |

### `admin.ManagePrimaryKeys`

```sql
EXEC [admin].[ManagePrimaryKeys] @Action = 'BACKUP'    -- snapshot current defs to [admin].[_PK_Backup]
EXEC [admin].[ManagePrimaryKeys] @Action = 'DROP'      -- drop all PKs/FKs
EXEC [admin].[ManagePrimaryKeys] @Action = 'RESTORE'   -- re-create PKs/FKs from backup
EXEC [admin].[ManagePrimaryKeys] @Action = 'RECREATE'  -- DROP then RESTORE
```

Use cases:
- Bulk-loading additional data into an already-imported database (drop, load, restore)
- Recovering constraints if `-DropPKBeforeLoad` ran without `-RestorePKAfterLoad`
- Resetting constraints to a known-good baseline after manual modifications

### `admin.ManageColumnstoreIndexes`

Creates or drops clustered columnstore indexes on fact tables. Same effect as the importer's `-ApplyCCI` flag, but runnable after the fact.

---

## Verification procedures

Data-integrity checks. Run individually for targeted validation, or use `verify.RunAll` for a complete sweep.

| Procedure | Description |
|---|---|
| `verify.RunAll` | Execute all verification checks and return a combined summary |
| `verify.CrossDimension` | FK integrity between dimension tables (geography → stores, etc.) |
| `verify.Customers` | Customer demographics: type distribution, household coverage, SCD2 validity |
| `verify.EmployeeStoreSales` | Employee-store assignment coverage and sales attribution |
| `verify.FactDistributions` | Sales amount, quantity, and discount statistical distributions |
| `verify.Geography` | Geography completeness: all countries, states, and cities populated |
| `verify.Products` | Product pricing sanity: margin ranges, active ratios, SCD2 consistency |
| `verify.SalesRelationships` | FK integrity from sales → all dimension tables |
| `verify.SecondaryFacts` | Budget, inventory, wishlists, and complaints row counts and FK checks |
| `verify.Stores` | Store types, opening/closing dates, online vs physical distribution |
| `verify.TemporalCoverage` | Date range coverage: sales span matches date dimension |
| `verify.Warehouses` | Warehouse-store assignments and geographic coverage |

### Running individual checks

```sql
-- Quickly check sales FK integrity after a partial reload
EXEC verify.SalesRelationships

-- Validate customer SCD2 after editing customer dimension
EXEC verify.Customers
```

### Running everything

```sql
EXEC verify.RunAll
```

`verify.RunAll` is what the importer's `-Verify` flag invokes. It returns a result set per check, plus a summary row indicating pass/fail counts.

---

## Typical workflows

### After importing with `-DropPKBeforeLoad` without `-RestorePKAfterLoad`
```sql
EXEC [admin].[ManagePrimaryKeys] @Action = 'RESTORE'
EXEC verify.RunAll
```

### After manually loading extra rows
```sql
EXEC [admin].[ManagePrimaryKeys] @Action = 'DROP'
-- ... your BULK INSERT / INSERT statements ...
EXEC [admin].[ManagePrimaryKeys] @Action = 'RESTORE'
EXEC verify.SalesRelationships
```

### Re-applying CCI after dropping for maintenance
```sql
EXEC [admin].[ManageColumnstoreIndexes] @Action = 'CREATE'
```

### Dropping CCI to allow large updates
```sql
EXEC [admin].[ManageColumnstoreIndexes] @Action = 'DROP'
-- ... your UPDATE / DELETE statements run faster on a heap ...
EXEC [admin].[ManageColumnstoreIndexes] @Action = 'CREATE'
```

### Rebuilding fragmented CCI (after many updates)
```sql
EXEC [admin].[ManageColumnstoreIndexes] @Action = 'REBUILD'
```

### Full reset to baseline after experimentation
```sql
-- Drop everything, reload, rebuild
EXEC [admin].[ManagePrimaryKeys] @Action = 'DROP'
EXEC [admin].[ManageColumnstoreIndexes] @Action = 'DROP'
-- ... reload data ...
EXEC [admin].[ManagePrimaryKeys] @Action = 'RESTORE'
EXEC [admin].[ManageColumnstoreIndexes] @Action = 'CREATE'
EXEC verify.RunAll
```

---

## Verification examples

### Quick "did the import succeed" sanity check
```sql
EXEC verify.RunAll
-- Returns one result set per check + a summary row
```

### Investigate sales FK orphans specifically
```sql
EXEC verify.SalesRelationships
```
Returns counts of sales rows with missing FK targets per dimension. Zero everywhere = clean.

### Check date dimension fully covers sales
```sql
EXEC verify.TemporalCoverage
```
Useful after manually narrowing the date dimension — confirms no sales fall outside the date range.

### Validate customer SCD2 versioning is consistent
```sql
EXEC verify.Customers
```
Catches overlapping SCD2 versions, missing current-version rows, and demographic distribution drift.

### Spot-check secondary facts after enabling a new feature
```sql
EXEC verify.SecondaryFacts
```
Returns row counts and FK integrity for budget, inventory, wishlists, and complaints.

### Run a focused subset
```sql
EXEC verify.Geography
EXEC verify.Stores
EXEC verify.Products
```
Useful after a `--regen-dimensions` run that only rebuilt a few dimensions.

### Scheduling regular validation (SQL Agent example)
```sql
-- In a SQL Agent job step:
USE [Sales1M];
EXEC verify.RunAll;
```
Run weekly to catch corruption from external modifications.
