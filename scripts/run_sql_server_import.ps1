# NOTE:
# This script is intended for CSV-based runs only.
# The run path must contain 'sql/schema/' and 'sql/load/' folders.

[CmdletBinding(DefaultParameterSetName = "Trusted")]
param (
    [Parameter(Mandatory = $true)]
    [string]$RunPath,

    [Parameter(Mandatory = $true)]
    [string]$Server,

    [Parameter(Mandatory = $true)]
    [string]$Database,

    # --- Auth (choose ONE mode) ---
    [Parameter(ParameterSetName = "Trusted")]
    [switch]$TrustedConnection,

    [Parameter(Mandatory = $true, ParameterSetName = "SqlAuth")]
    [string]$User,

    [Parameter(Mandatory = $true, ParameterSetName = "SqlAuth")]
    [string]$Password,

    # --- Optional flags ---
    [bool]$ApplyCCI = $false,

    # Drop primary key and foreign key constraints after import (reduces size)
    [bool]$DropPK = $false,

    # Drop PKs and FKs BEFORE the load so BULK INSERT runs into pure heaps.
    # Removes per-row PK maintenance and FK validation that bottlenecks
    # parallel loads. Definitions are saved to [admin].[_PK_Backup]; pair
    # with -RestorePKAfterLoad to re-add them automatically post-load.
    [switch]$DropPKBeforeLoad,

    # Restore PKs/FKs from [admin].[_PK_Backup] after the load (and after
    # CCI apply if -ApplyCCI). Requires -DropPKBeforeLoad. Cannot be
    # combined with -DropPK (conflicting end-state).
    [switch]$RestorePKAfterLoad,

    # Run data verification after import (EXEC verify.RunAll)
    [switch]$Verify,

    # Provision a SQL login + DB user with DB_OWNER for SSAS/Power BI access.
    # Password resolution order: -TabularPassword > $env:SYNDATA_TABULAR_PASSWORD > interactive prompt.
    [switch]$ProvisionTabularUser,

    # Login name for the tabular user (default: tabular_user).
    # Used as both the SQL login and the per-DB user. Letters/digits/underscores only.
    [string]$TabularLogin = "tabular_user",

    # SecureString password for the tabular user. Pass via:
    #   $sec = Read-Host -AsSecureString "Enter tabular password"
    #   .\run_sql_server_import.ps1 ... -ProvisionTabularUser -TabularPassword $sec
    [SecureString]$TabularPassword,

    # e.g. "ODBC Driver 18 for SQL Server"
    [string]$OdbcDriver,

    # Number of parallel BULK INSERT workers for multi-chunk fact tables.
    # Each worker holds its own connection and loads chunks concurrently
    # into the same heap (TABLOCK allows this). 1 disables parallelism.
    # Default 4. Diminishing returns past ~8 on a single NVMe.
    [int]$LoadWorkers = 4,

    # Override python executable (defaults to active venv python if venv is activated)
    [string]$PythonExe = "python",

    # Show full absolute paths in the "Running:" line
    [switch]$ShowFullPaths
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "_common.ps1")

try {
    $ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
    $RepoRoot   = (Resolve-Path (Join-Path $ScriptRoot "..")).Path

    $PyEntrypoint = Join-Path $RepoRoot "scripts\sql\run_sql_server_import.py"
    if (-not (Test-Path $PyEntrypoint)) {
        throw "Python entrypoint not found: $PyEntrypoint"
    }

    $ResolvedRunPath = (Resolve-Path $RunPath).Path

    # CSV-only guard
    $schemaDir = Join-Path $ResolvedRunPath "sql\schema"
    $loadDir   = Join-Path $ResolvedRunPath "sql\load"
    if (-not (Test-Path $schemaDir) -or -not (Test-Path $loadDir)) {
        throw "CSV run required. Expected sql/schema/ and sql/load/ folders in: $ResolvedRunPath"
    }
    if ((Get-ChildItem -Path $schemaDir -File | Measure-Object).Count -eq 0) {
        throw "sql/schema/ folder is empty - no SQL scripts to execute in: $schemaDir"
    }
    if ((Get-ChildItem -Path $loadDir -File | Measure-Object).Count -eq 0) {
        throw "sql/load/ folder is empty - no data files to load in: $loadDir"
    }

    # Log the resolved python interpreter and version
    $pyResolvedPath = (Get-Command $PythonExe -ErrorAction SilentlyContinue).Source
    if ($pyResolvedPath) {
        $pyVer = & $PythonExe -c "import sys; v=sys.version_info; print(str(v.major)+'.'+str(v.minor)+'.'+str(v.micro))" 2>$null
        Write-Step "Python : $($pyVer.Trim())  [$pyResolvedPath]"
    } else {
        Write-Step "Python : $PythonExe (not resolved on PATH)" -Level warn
    }

    # Build argument list (array form handles spaces safely)
    $argsList = @(
        $PyEntrypoint,
        "--server",   $Server,
        "--database", $Database,
        "--run-path", $ResolvedRunPath
    )

    if ($PSCmdlet.ParameterSetName -eq "Trusted") {
        $argsList += "--trusted"
    } else {
        $argsList += @("--user", $User, "--password", $Password)
    }

    if ($ApplyCCI) { $argsList += "--apply-cci" }
    if ($DropPK)   { $argsList += "--drop-pk" }
    if ($DropPKBeforeLoad)  { $argsList += "--drop-pk-before-load" }
    if ($RestorePKAfterLoad) { $argsList += "--restore-pk-after-load" }
    if ($Verify)   { $argsList += "--verify" }
    if ($ProvisionTabularUser) {
        # Validate login name (must match the Python-side regex)
        if ($TabularLogin -notmatch '^[A-Za-z_][A-Za-z0-9_]{0,127}$') {
            throw "-TabularLogin '$TabularLogin' is invalid. Use letters, digits, underscores only (max 128 chars)."
        }

        # Resolve password: -TabularPassword > env var > prompt (interactive only)
        $resolvedPassword = $null
        if ($TabularPassword) {
            $resolvedPassword = [System.Net.NetworkCredential]::new('', $TabularPassword).Password
        } elseif ($env:SYNDATA_TABULAR_PASSWORD) {
            $resolvedPassword = $env:SYNDATA_TABULAR_PASSWORD
        } elseif ([Environment]::UserInteractive -and -not [Console]::IsInputRedirected) {
            $promptedSec = Read-Host -AsSecureString "Enter password for tabular login [$TabularLogin]"
            $resolvedPassword = [System.Net.NetworkCredential]::new('', $promptedSec).Password
        } else {
            throw "-ProvisionTabularUser needs a password. Pass -TabularPassword, set `$env:SYNDATA_TABULAR_PASSWORD, or run interactively."
        }

        # Forward to the python child via env var (avoids process-listing exposure)
        $env:SYNDATA_TABULAR_PASSWORD = $resolvedPassword

        $argsList += @("--provision-tabular-user", "--tabular-login", $TabularLogin)
    }
    if ($OdbcDriver) { $argsList += @("--odbc-driver", $OdbcDriver) }

    if ($LoadWorkers -lt 1) {
        throw "-LoadWorkers must be >= 1 (got $LoadWorkers)."
    }
    $argsList += @("--load-workers", $LoadWorkers.ToString())

    # Log the command
    # Mask password in logged output
    $logArgsList = @($argsList)
    for ($i = 0; $i -lt $logArgsList.Count; $i++) {
        if ($logArgsList[$i] -eq "--password" -and ($i + 1) -lt $logArgsList.Count) {
            $logArgsList[$i + 1] = "********"
        }
    }

    if ($ShowFullPaths) {
        Write-Step ("Running: {0} {1}" -f $PythonExe, ($logArgsList -join " ")) -Level cmd
    } else {
        $pyName    = Split-Path -Leaf $PythonExe
        $entryName = Split-Path -Leaf $PyEntrypoint

        $runDisplay = $ResolvedRunPath
        if ($ResolvedRunPath.Length -gt $RepoRoot.Length -and
            $ResolvedRunPath.StartsWith($RepoRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            $runDisplay = $ResolvedRunPath.Substring($RepoRoot.Length).TrimStart('\')
        } elseif ($ResolvedRunPath -ne $RepoRoot) {
            $runDisplay = Split-Path -Leaf $ResolvedRunPath
        }

        $authLabel = if ($PSCmdlet.ParameterSetName -eq "Trusted") { "trusted" } else { "sql-auth" }

        $flags = @()
        if ($ApplyCCI) { $flags += "apply-cci" }
        if ($DropPK)   { $flags += "drop-pk" }
        if ($DropPKBeforeLoad)  { $flags += "drop-pk-before-load" }
        if ($RestorePKAfterLoad) { $flags += "restore-pk-after-load" }
        if ($Verify)   { $flags += "verify" }
        if ($ProvisionTabularUser) { $flags += ("provision-tabular-user=" + $TabularLogin) }
        if ($OdbcDriver) { $flags += ("odbc=" + $OdbcDriver) }
        $flags += ("load-workers=" + $LoadWorkers)

        $flagText = if ($flags.Count -gt 0) { " [" + ($flags -join ", ") + "]" } else { "" }

        Write-Step ("Running: {0} {1} --server {2} --database {3} --run {4} ({5}){6}" -f $pyName, $entryName, $Server, $Database, $runDisplay, $authLabel, $flagText) -Level cmd
    }

    # Execute
    & $PythonExe @argsList
    $ec = $LASTEXITCODE
    if ($ec -ne 0) {
        Write-Step "SQL Server import exited with code $ec." -Level err
    }
    exit $ec
}
catch {
    Write-Step "run_sql_server_import failed: $($_.Exception.Message)" -Level err
    exit 1
}
