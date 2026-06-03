# ------------------------------------------------------------
# Sync (create/update) a project virtual environment
#
# Behavior:
#   - Uses uv exclusively for locked, reproducible installs from uv.lock
#   - Resolves project root (walks upward for .git / pyproject.toml)
#   - Creates the venv if missing (or recreates with -Force)
#   - uv only reinstalls when the lockfile changed (native incremental sync)
#   - If uv is not installed, the script stops and tells you how to get it
#
# Exit codes:
#   0 = OK
#   1 = failure (uv missing, uv sync failed)
#
# Usage:
#   .\scripts\sync_venv.ps1
#   .\scripts\sync_venv.ps1 -Force
#   .\scripts\sync_venv.ps1 -Dev
#   .\scripts\sync_venv.ps1 -Minimal             # core deps only, no import drivers
#   .\scripts\sync_venv.ps1 -Extras sql          # only the SQL Server import driver
#   .\scripts\sync_venv.ps1 -Quiet
#
# Extras (sql, postgres) install by default; -Minimal skips them. See README.
# ------------------------------------------------------------

[CmdletBinding()]
param(
    [string]$VenvDir = ".venv",
    [switch]$Force,
    [switch]$Dev,
    # Optional-dependency extras to install (installed by default).
    [string[]]$Extras = @("sql", "postgres"),
    [switch]$Minimal,
    [switch]$Quiet,
    [string]$ProjectRoot
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

. (Join-Path $PSScriptRoot "_common.ps1")

# Helper to conditionally log
function Log {
    param([string]$Msg, [string]$Level = "info")
    if (-not $Quiet) { Write-Step $Msg -Level $Level }
}

try {
    $RootPath = if ($ProjectRoot) { (Resolve-Path $ProjectRoot).Path } else { Resolve-ProjectRoot -StartDir $PSScriptRoot }
    $VenvPath = Join-Path $RootPath $VenvDir

    Log "Project root  : $RootPath"
    Log "Venv path     : $VenvPath"

    # Require uv — this project standardizes on uv for reproducible installs.
    Assert-UvAvailable

    # Handle -Force: remove existing venv
    if ($Force -and (Test-Path -LiteralPath $VenvPath)) {
        Log "Removing existing venv (-Force)..." -Level warn
        Remove-Item -LiteralPath $VenvPath -Recurse -Force
    }

    Log "Using uv sync (locked dependencies)" -Level ok

    $uvArgs = Build-UvSyncArgs -ProjectRoot $RootPath -Dev:$Dev -Minimal:$Minimal -Extras $Extras -Quiet:$Quiet
    Invoke-Checked -Exe "uv" -Arguments $uvArgs -ErrorMessage "uv sync failed."
    Log "Virtual environment synced successfully (uv, locked)." -Level ok
    exit 0
}
catch {
    Log "sync_venv failed: $($_.Exception.Message)" -Level err
    exit 1
}
