# Provisioning a Tabular User

When importing into SSAS Tabular or Power BI, a dedicated SQL login is usually expected so the analytics engine doesn't run under your personal credentials. The `-ProvisionTabularUser` flag on `run_sql_server_import.ps1` ensures one exists and is mapped into the imported database with `DB_OWNER`.

The same login can be reused across every imported database — re-running the importer with `-ProvisionTabularUser` on a new database just adds a user mapping; it doesn't recreate the server-level login.

---

## Quick recipes

### Interactive (most common)
Prompts for the password at run time:
```powershell
.\scripts\run_sql_server_import.ps1 `
  -RunPath ".\generated_datasets\<your-run-folder>" `
  -Server "SUMMER\SQL2025" `
  -Database Sales2M `
  -TrustedConnection `
  -ApplyCCI $true `
  -DropPKBeforeLoad `
  -RestorePKAfterLoad `
  -LoadWorkers 8 `
  -Verify `
  -ProvisionTabularUser `
  -TabularLogin tabular_user
# → Prompts: Enter password for tabular login [tabular_user]
```

### Pre-supplied password (scripted but still secure)
```powershell
$sec = Read-Host -AsSecureString "Tabular password"

.\scripts\run_sql_server_import.ps1 `
  -RunPath ".\generated_datasets\<your-run-folder>" `
  -Server "SUMMER\SQL2025" `
  -Database Sales2M `
  -TrustedConnection `
  -ApplyCCI $true `
  -DropPKBeforeLoad `
  -RestorePKAfterLoad `
  -LoadWorkers 8 `
  -Verify `
  -ProvisionTabularUser `
  -TabularLogin tabular_user `
  -TabularPassword $sec
```

### Automation / CI
```powershell
$env:SYNDATA_TABULAR_PASSWORD = "..."

.\scripts\run_sql_server_import.ps1 `
  -RunPath ".\generated_datasets\<your-run-folder>" `
  -Server "SUMMER\SQL2025" `
  -Database Sales2M `
  -TrustedConnection `
  -ApplyCCI $true `
  -DropPKBeforeLoad `
  -RestorePKAfterLoad `
  -LoadWorkers 8 `
  -Verify `
  -ProvisionTabularUser `
  -TabularLogin analytics_user
```

### Default login name (no `-TabularLogin` flag)
```powershell
.\scripts\run_sql_server_import.ps1 `
  -RunPath ".\generated_datasets\<your-run-folder>" `
  -Server "SUMMER\SQL2025" `
  -Database SalesDemo `
  -TrustedConnection `
  -ProvisionTabularUser
# → Uses default login name: tabular_user
# → Prompts for password interactively
```

### Provision a per-environment login
```powershell
# Dev environment
.\scripts\run_sql_server_import.ps1 -Database Sales_Dev ... -ProvisionTabularUser -TabularLogin pbi_dev_user

# Staging environment
.\scripts\run_sql_server_import.ps1 -Database Sales_Stg ... -ProvisionTabularUser -TabularLogin pbi_stg_user

# Production environment
.\scripts\run_sql_server_import.ps1 -Database Sales_Prd ... -ProvisionTabularUser -TabularLogin pbi_prd_user
```

### Reuse the same login across many databases
```powershell
# First database — creates the login + maps it
.\scripts\run_sql_server_import.ps1 -Database Sales1M  ... -ProvisionTabularUser -TabularLogin tabular_user

# Subsequent databases — finds existing login, just adds a user mapping
.\scripts\run_sql_server_import.ps1 -Database Sales10M ... -ProvisionTabularUser -TabularLogin tabular_user
.\scripts\run_sql_server_import.ps1 -Database Sales100M ... -ProvisionTabularUser -TabularLogin tabular_user
```
The password is only used the very first time. Subsequent runs don't change the existing login's password.

---

## Manual operations on the tabular login

The script handles the common cases, but here's the SQL for direct management.

### Change the password later
```sql
ALTER LOGIN [tabular_user] WITH PASSWORD = 'NewSecurePassword!';
```

### Lock or disable the login
```sql
ALTER LOGIN [tabular_user] DISABLE;
-- or
ALTER LOGIN [tabular_user] ENABLE;
```

### Remove the user from a specific database (keep login)
```sql
USE [Sales1M];
DROP USER [tabular_user];
```

### Remove the login entirely (drops from all databases automatically)
```sql
-- First remove the user mapping from each database that uses it
USE [Sales1M]; DROP USER [tabular_user];
USE [Sales10M]; DROP USER [tabular_user];

-- Then drop the server-level login
USE [master];
DROP LOGIN [tabular_user];
```

### Verify the login + user mapping exist
```sql
-- Server-level: does the login exist?
SELECT name, type_desc, is_disabled FROM sys.server_principals WHERE name = 'tabular_user';

-- DB-level: is the user mapped here with db_owner?
USE [Sales1M];
SELECT u.name, u.type_desc, r.name AS role
FROM sys.database_principals u
JOIN sys.database_role_members rm ON rm.member_principal_id = u.principal_id
JOIN sys.database_principals r ON r.principal_id = rm.role_principal_id
WHERE u.name = 'tabular_user';
```

---

## Relevant flags

| Flag | Description |
|---|---|
| `-ProvisionTabularUser` | Enable provisioning. After import, ensure a SQL login + DB user exists with `DB_OWNER`. |
| `-TabularLogin` | Login name (default: `tabular_user`). Must match regex `^[A-Za-z_][A-Za-z0-9_]{0,127}$`. |
| `-TabularPassword` | `SecureString` password. Alternative: `$env:SYNDATA_TABULAR_PASSWORD`. |

---

## Password resolution order

The script picks up the password in this order, stopping at the first source that provides one:

1. `-TabularPassword` parameter (SecureString)
2. `$env:SYNDATA_TABULAR_PASSWORD` environment variable
3. Interactive prompt (only if the session is interactive)
4. **Error** — if none of the above apply (e.g. non-interactive run with no password set)

---

## Required SQL Server permissions

The importing connection needs:

- **Server-level rights** the *first time* the login is created (e.g. `securityadmin` role, or `sysadmin`). `CREATE LOGIN` requires server-scope permissions.
- **DB-level rights** for subsequent runs that only need to add a user mapping in a new database (`db_owner` on the new DB is sufficient if the login already exists).

If the importing connection lacks server-level rights, provisioning will fail but **the import itself succeeds** — the failure is logged as a warning and the script continues. You can manually create the login once with `sa` or another privileged account, and subsequent provisioning calls (which only need to add a user mapping) will work.

---

## What the script actually does

1. Checks whether a server-level login matching `-TabularLogin` exists. If not, creates it with the supplied password.
2. Checks whether a database user mapped to that login exists in the target database. If not, creates the user.
3. Grants the user `DB_OWNER` role in the target database.

The `DB_OWNER` grant is intentional: SSAS Tabular / Power BI processing operations need broad permissions on the source database. If you need a more locked-down role, create the user manually and skip `-ProvisionTabularUser`.

---

## Reusing the same login across databases

The typical workflow is to provision the login once and reuse it for every imported dataset:

```powershell
# First import — creates the login + maps it to Sales1M
.\scripts\run_sql_server_import.ps1 -Database Sales1M ... -ProvisionTabularUser -TabularLogin tabular_user

# Subsequent imports — login already exists, just adds a user mapping in Sales20M
.\scripts\run_sql_server_import.ps1 -Database Sales20M ... -ProvisionTabularUser -TabularLogin tabular_user
```

The password is only used the first time, when creating the login. Subsequent runs that find the login already in place don't change its password.

---

## Troubleshooting

**"Provisioning failed, continuing" warning in the log**
The importing connection lacks server-level rights to create the login. Either run once with `sa`, or have a DBA create the login manually, then re-run.

**Login regex validation error**
Login name must start with a letter or underscore, contain only letters/digits/underscores, and be 1–128 chars. Adjust `-TabularLogin`.

**"Password does not meet complexity requirements"**
SQL Server enforces password policy when creating a login under a Windows policy. Use a longer password with mixed case + digits + symbols.

**Cannot connect to the database as the tabular user after provisioning**
Confirm Mixed Mode authentication is enabled on the SQL Server instance — `-TrustedConnection` doesn't require this, but logging in as the tabular user does.
