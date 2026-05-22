# NOTE:
# This script is intended for CSV-based runs only.
# The run path must contain 'postgres/schema/' and (optionally) 'postgres/load/' folders.
#
# Mirrors run_sql_server_import.ps1. Drop / restore / CCI / tabular-user
# flags from the SQL Server wrapper are intentionally absent — Postgres
# doesn't need them (server-side COPY doesn't have the parallel-BULK-INSERT
# contention that motivated PK drop/restore on SQL Server).

[CmdletBinding()]
param (
    [Parameter(Mandatory = $true)]
    [string]$RunPath,

    [Parameter(Mandatory = $true)]
    [string]$Database,

    # Postgres host. Named -PgHost because -Host is reserved in PowerShell.
    [string]$PgHost = "localhost",

    [int]$Port = 5432,

    # Postgres role. Named -Username because -User collides with the SQL
    # Server wrapper's auth mode.
    [string]$Username = "postgres",

    # SecureString password. Pass via:
    #   $sec = Read-Host -AsSecureString "Postgres password"
    #   .\run_postgres_import.ps1 ... -Password $sec
    # Resolution order: -Password > $env:PGPASSWORD > interactive prompt.
    [SecureString]$Password,

    # The per-table row-count summary runs by default (matches the Python
    # entrypoint and import_postgres()). -Verify is accepted as a no-op for
    # script-style consistency with run_sql_server_import.ps1; pass
    # -NoVerify to skip it.
    [switch]$Verify,
    [switch]$NoVerify,

    [string]$PythonExe = "python",

    [switch]$ShowFullPaths
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "_common.ps1")

try {
    $ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
    $RepoRoot   = (Resolve-Path (Join-Path $ScriptRoot "..")).Path

    $PyEntrypoint = Join-Path $RepoRoot "scripts\sql\run_postgres_import.py"
    if (-not (Test-Path $PyEntrypoint)) {
        throw "Python entrypoint not found: $PyEntrypoint"
    }

    $ResolvedRunPath = (Resolve-Path $RunPath).Path

    $schemaDir = Join-Path $ResolvedRunPath "postgres\schema"
    if (-not (Test-Path $schemaDir)) {
        throw "Postgres CSV run required. Expected 'postgres/schema/' under: $ResolvedRunPath"
    }
    if ((Get-ChildItem -Path $schemaDir -File -Filter *.sql | Measure-Object).Count -eq 0) {
        throw "postgres/schema/ folder is empty - no SQL scripts to execute in: $schemaDir"
    }

    # Log the resolved python interpreter and version
    $pyResolvedPath = (Get-Command $PythonExe -ErrorAction SilentlyContinue).Source
    if ($pyResolvedPath) {
        $pyVer = & $PythonExe -c "import sys; v=sys.version_info; print(str(v.major)+'.'+str(v.minor)+'.'+str(v.micro))" 2>$null
        Write-Step "Python : $($pyVer.Trim())  [$pyResolvedPath]"
    } else {
        Write-Step "Python : $PythonExe (not resolved on PATH)" -Level warn
    }

    # Resolve password: -Password > $env:PGPASSWORD > prompt (interactive only)
    $resolvedPassword = $null
    if ($Password) {
        $resolvedPassword = [System.Net.NetworkCredential]::new('', $Password).Password
    } elseif ($env:PGPASSWORD) {
        $resolvedPassword = $env:PGPASSWORD
    } elseif ([Environment]::UserInteractive -and -not [Console]::IsInputRedirected) {
        $promptedSec = Read-Host -AsSecureString "Postgres password for $Username@${PgHost}:$Port"
        $resolvedPassword = [System.Net.NetworkCredential]::new('', $promptedSec).Password
    } else {
        throw "Postgres password not provided. Pass -Password, set `$env:PGPASSWORD, or run interactively."
    }

    # Forward to the Python child via env var rather than argv to avoid
    # exposing the password in process listings. Snapshot the prior value
    # so the finally block below can restore the caller's environment
    # instead of leaking the password to the parent shell.
    $prevPgPassword = $env:PGPASSWORD
    $env:PGPASSWORD = $resolvedPassword

    $argsList = @(
        $PyEntrypoint,
        "--host",     $PgHost,
        "--port",     $Port.ToString(),
        "--database", $Database,
        "--user",     $Username,
        "--run-path", $ResolvedRunPath
    )
    if ($NoVerify) { $argsList += "--no-verify" }

    if ($ShowFullPaths) {
        Write-Step ("Running: {0} {1}" -f $PythonExe, ($argsList -join " ")) -Level cmd
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

        $flags = @()
        if ($NoVerify) { $flags += "no-verify" }
        $flagText = if ($flags.Count -gt 0) { " [" + ($flags -join ", ") + "]" } else { "" }

        Write-Step ("Running: {0} {1} --host {2}:{3} --database {4} --run {5}{6}" -f $pyName, $entryName, $PgHost, $Port, $Database, $runDisplay, $flagText) -Level cmd
    }

    try {
        & $PythonExe @argsList
        $ec = $LASTEXITCODE
    } finally {
        # Restore the caller's PGPASSWORD (typically empty) so the password
        # we resolved above doesn't leak into the parent shell.
        $env:PGPASSWORD = $prevPgPassword
    }
    if ($ec -ne 0) {
        Write-Step "Postgres import exited with code $ec." -Level err
    }
    exit $ec
}
catch {
    Write-Step "run_postgres_import failed: $($_.Exception.Message)" -Level err
    exit 1
}
