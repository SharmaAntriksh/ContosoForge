# SQL Server Import

Load a CSV-format generated dataset into SQL Server in one step. The import script reads the SQL scripts that were generated alongside the CSVs (`schema/`, `load/`, `indexes/`) and executes them with configurable parallelism, constraint handling, and post-load verification.

> Available only for `--format csv` runs. Parquet and Delta outputs do not generate SQL bootstrap scripts.

> If the target database already exists, the import is skipped automatically. Use a fresh database name per run, or drop the database first.

---

## Quick recipes

### Fast load, Windows Authentication (recommended for SSAS / Power BI)
```powershell
.\scripts\run_sql_server_import.ps1 `
  -RunPath ".\generated_datasets\2026-03-26 06_40_28 PM Customers 29K Sales 1M CSV" `
  -Server "YOURSERVER\SQL2022" `
  -Database Sales1M `
  -TrustedConnection `
  -ApplyCCI $true `
  -DropPKBeforeLoad `
  -RestorePKAfterLoad `
  -LoadWorkers 8 `
  -Verify
```

### SQL Authentication
```powershell
.\scripts\run_sql_server_import.ps1 `
  -RunPath ".\generated_datasets\<your-run-folder>" `
  -Server "YOURSERVER\SQL2022" `
  -Database ContosoSales `
  -User sa `
  -Password "YourPassword"
```

> SQL Authentication requires Mixed Mode to be enabled on the SQL Server instance.

### Defensive load (validate every row against PKs/FKs as it lands)
```powershell
.\scripts\run_sql_server_import.ps1 `
  -RunPath ".\generated_datasets\<your-run-folder>" `
  -Server "YOURSERVER\SQL2022" `
  -Database SalesDebug `
  -TrustedConnection
```
No PK flags = constraints stay active during load. Fails fast on bad data.

### Smallest possible final database (no PKs/FKs after load)
```powershell
.\scripts\run_sql_server_import.ps1 `
  -RunPath ".\generated_datasets\<your-run-folder>" `
  -Server "YOURSERVER\SQL2022" `
  -Database SalesAnalytics `
  -TrustedConnection `
  -ApplyCCI $true `
  -DropPKBeforeLoad `
  -LoadWorkers 8 `
  -Verify
```
Notice the missing `-RestorePKAfterLoad` — constraints stay dropped. CCI alone is enough for analytical workloads.

### Fast load + provision a tabular login for Power BI
```powershell
.\scripts\run_sql_server_import.ps1 `
  -RunPath ".\generated_datasets\<your-run-folder>" `
  -Server "YOURSERVER\SQL2022" `
  -Database SalesPBI `
  -TrustedConnection `
  -ApplyCCI $true `
  -DropPKBeforeLoad `
  -RestorePKAfterLoad `
  -LoadWorkers 8 `
  -Verify `
  -ProvisionTabularUser `
  -TabularLogin tabular_user
```
See [tabular-user.md](./tabular-user.md) for password-handling modes.

### Iterating quickly while debugging generator changes
```powershell
.\scripts\run_sql_server_import.ps1 `
  -RunPath ".\generated_datasets\<your-run-folder>" `
  -Server "YOURSERVER\SQL2022" `
  -Database SalesIterate `
  -TrustedConnection `
  -LoadWorkers 4
```
No CCI (skip the expensive index build), constraints active (catch generator bugs at load time), moderate parallelism.

### Importing the same run into multiple databases
```powershell
# Same RunPath, different database names — useful for A/B testing query patterns
.\scripts\run_sql_server_import.ps1 -RunPath ".\generated_datasets\<run>" -Server "SRV\SQL" -Database Sales_NoCCI -TrustedConnection
.\scripts\run_sql_server_import.ps1 -RunPath ".\generated_datasets\<run>" -Server "SRV\SQL" -Database Sales_CCI    -TrustedConnection -ApplyCCI $true -DropPKBeforeLoad -RestorePKAfterLoad
```

### Conservative load on an HDD-backed instance
```powershell
.\scripts\run_sql_server_import.ps1 `
  -RunPath ".\generated_datasets\<your-run-folder>" `
  -Server "YOURSERVER\SQL2022" `
  -Database SalesHDD `
  -TrustedConnection `
  -LoadWorkers 2
```
On spinning disks, parallel workers cause seek thrashing. Use 2 workers max.

### Maximum-throughput load on a beefy NVMe box
```powershell
.\scripts\run_sql_server_import.ps1 `
  -RunPath ".\generated_datasets\<your-run-folder>" `
  -Server "BEEFY\SQL2022" `
  -Database SalesBig `
  -TrustedConnection `
  -ApplyCCI $true `
  -DropPKBeforeLoad `
  -RestorePKAfterLoad `
  -LoadWorkers 12 `
  -Verify
```

---

## Full flag reference

| Flag | Description |
|---|---|
| `-RunPath` | Path to the generated dataset folder (the one with `dimensions/`, `facts/`, `sql/`) |
| `-Server` | SQL Server instance, e.g. `SERVERNAME\SQL2022` |
| `-Database` | Target database name. Created if absent; skipped if present. |
| `-TrustedConnection` | Use Windows Authentication |
| `-User` / `-Password` | SQL Authentication credentials (alternative to `-TrustedConnection`) |
| `-ApplyCCI $true` | Create clustered columnstore indexes on fact tables after load |
| `-DropPKBeforeLoad` | Drop PKs and FKs **before** the data load. Removes per-row validation overhead. Definitions saved to `[admin].[_PK_Backup]`. |
| `-RestorePKAfterLoad` | Restore PKs and FKs from `[admin].[_PK_Backup]` after load (and after CCI apply). Requires `-DropPKBeforeLoad`. Cannot be combined with `-DropPK`. |
| `-DropPK $true` | Drop PKs and FKs **after** load (analytics-only end state, smallest DB). Keeps constraints active during the load. |
| `-LoadWorkers <N>` | Parallel `BULK INSERT` worker count for multi-chunk fact tables (default `4`). Each worker holds its own pyodbc connection. |
| `-Verify` | Run post-import data integrity checks (executes `verify.RunAll`) |
| `-ProvisionTabularUser` | After import, ensure a SQL login + DB user exists with `DB_OWNER`. See [tabular-user.md](./tabular-user.md). |
| `-TabularLogin` | Login name for the tabular user (default: `tabular_user`) |
| `-TabularPassword` | `SecureString` password for the tabular user |

---

## Pick the right flag combination for your end-state

| Goal | Flags | Notes |
|---|---|---|
| **Fast load + PKs/FKs intact + CCI** (recommended for SSAS / Power BI) | `-ApplyCCI $true -DropPKBeforeLoad -RestorePKAfterLoad -LoadWorkers 8` | Fastest end-to-end. Pre-drops constraints, parallel-loads heaps, applies CCI, re-adds PKs/FKs. |
| **Fast load, analytics-only, smallest DB** | `-ApplyCCI $true -DropPKBeforeLoad -LoadWorkers 8` | Same fast load, but PKs/FKs stay dropped. Smallest final size. Can be restored later with `EXEC [admin].[ManagePrimaryKeys] @Action = 'RESTORE'`. |
| **Safe load with row-by-row constraint validation** | (no PK flags) | Default behavior. PKs/FKs validated as data arrives. Slowest at scale, but defensive. |
| **Defensive load + analytics-only end state** | `-ApplyCCI $true -DropPK $true` | Validates every row against PKs/FKs as it loads, then drops constraints for a small final DB. Use when CSVs are untrusted (manually edited, debugging generator changes, third-party data) — fails immediately on duplicates or orphan FKs instead of late at the restore step. Trades load speed for early-failure feedback. |
| **Iterating on generator changes** | `-ApplyCCI $false` (default), no PK flags | Skip CCI build to shorten iteration loop. Constraint validation catches generator bugs early. |

For a 200M-row Sales fact table on a typical NVMe box with 8 cores, the fast-load preset cuts total import time from ~25 min (default behavior) to **~10 min** end-to-end. The biggest contributor is dropping FKs before load — Sales has 11 FK constraints, each adding per-row dimension lookups during `BULK INSERT`.

---

## Why `-DropPKBeforeLoad` is faster than `-DropPK`

`-DropPK` drops constraints **after** the load. During the load, SQL Server still validates every row against PKs (uniqueness checks on the clustered index) and every FK (lookup into the parent dimension). At scale, this dominates load time — the BULK INSERT is no longer bandwidth-bound, it's CPU-bound on validation.

`-DropPKBeforeLoad` drops constraints **before** the load, turning each target table into a heap with no FK relationships. `BULK INSERT` then runs at raw write speed. The restore step (`-RestorePKAfterLoad`) re-adds PKs and FKs from `[admin].[_PK_Backup]` after the data is in place — at this point SQL Server can validate against fully-populated tables in bulk, which is much faster than row-by-row validation during load.

The trade-off: if the data has bad rows (duplicates, orphan FKs), you find out at restore time rather than load time. For trusted generator output this is fine. For untrusted/edited CSVs, prefer the defensive variant.

---

## Worker tuning

The default `-LoadWorkers 4` is conservative. Practical guidance:

| Storage / cores | Recommended `-LoadWorkers` |
|---|---|
| HDD, any core count | 2 — disk seeks dominate, parallelism hurts |
| SATA SSD, 4–8 cores | 4 |
| NVMe, 8+ cores | 8 |
| NVMe, 16+ cores, dedicated SQL box | 8–12 (diminishing returns past ~8) |

Each worker holds its own pyodbc connection. Past ~8 workers on a single NVMe device, contention on the transaction log usually outweighs parallelism gains.

---

## Manual constraint recovery

Constraints saved to `[admin].[_PK_Backup]` by `-DropPKBeforeLoad` can be restored at any time without re-importing:

```sql
EXEC [admin].[ManagePrimaryKeys] @Action = 'RESTORE'
```

Other actions on the same procedure:

| `@Action` | Effect |
|---|---|
| `BACKUP` | Save current PK/FK definitions to `[admin].[_PK_Backup]` |
| `DROP` | Drop all PKs and FKs (use `BACKUP` first if you want to restore later) |
| `RESTORE` | Re-create PKs and FKs from `[admin].[_PK_Backup]` |
| `RECREATE` | `DROP` then `RESTORE` (resets constraints to backed-up state) |

---

## Provisioning a tabular user

For SSAS Tabular / Power BI usage, a dedicated SQL login is usually expected. The import script can create one for you with `-ProvisionTabularUser`. See [tabular-user.md](./tabular-user.md) for full coverage.

---

## Post-import stored procedures

The import generates stored procedures in the `admin` and `verify` schemas for ongoing management and validation. See [post-import-procedures.md](./post-import-procedures.md) for the full catalog.

---

## Troubleshooting

**"Database already exists, skipping import"**
Drop the database first, or use a different `-Database` name.

**`BULK INSERT` fails with code-page errors**
Generated CSVs are UTF-8. Make sure your SQL Server collation supports Unicode (modern installs do by default). The generated `BULK INSERT` statements use `CODEPAGE = '65001'`.

**`-RestorePKAfterLoad` fails with duplicate-key errors**
The data has duplicate PKs. This is the trade-off of pre-dropping constraints — bad rows aren't caught until restore. Investigate the source CSVs, fix, and retry. For repeated debugging cycles, switch to defensive load (no PK flags).

**`-ProvisionTabularUser` logs a warning but the import succeeded**
Provisioning failures are non-fatal. The importing connection probably lacks server-level rights (e.g. `securityadmin`). Create the login manually once and re-run; subsequent runs only need DB-level rights to map the user.

**Import is much slower than expected**
Check that `-DropPKBeforeLoad` is actually passed. The default (no PK flags) keeps constraints active and is the slow path. Also confirm `-LoadWorkers` is set appropriately for your storage.

**Out-of-memory or transaction-log full**
Lower `-LoadWorkers`. Fewer concurrent inserts means smaller peak transaction log usage.
