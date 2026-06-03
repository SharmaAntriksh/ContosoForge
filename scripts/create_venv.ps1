# ------------------------------------------------------------
# Create virtual environment (one-time setup)
# Always creates .venv at project root by default.
# DOES NOT activate the environment.
#
# Uses uv exclusively for locked, reproducible installs from uv.lock.
# If uv is not installed, the script stops and tells you how to get it.
#
# Usage:
#   .\scripts\create_venv.ps1
#   .\scripts\create_venv.ps1 -Force
#   .\scripts\create_venv.ps1 -Dev
#   .\scripts\create_venv.ps1 -Minimal            # core deps only, no import drivers
#   .\scripts\create_venv.ps1 -Extras sql         # only the SQL Server import driver
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
    [switch]$Minimal
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

. (Join-Path $PSScriptRoot "_common.ps1")

try {
    $ProjectRoot = Resolve-ProjectRoot -StartDir $PSScriptRoot
    $VenvPath    = Join-Path $ProjectRoot $VenvDir

    Write-Step "Project root : $ProjectRoot"
    Write-Step "Venv target  : $VenvPath"

    # Require uv — this project standardizes on uv for reproducible installs.
    Assert-UvAvailable

    # Guard: already exists (unless -Force)
    if (Test-Path $VenvPath) {
        if (-not $Force) {
            Write-Step "Virtual environment already exists at $VenvPath" -Level warn
            Write-Step "Use -Force to recreate it, or run sync_venv.ps1 to update it." -Level info
            return
        }
        Write-Step "Removing existing venv (-Force)..." -Level warn
        Remove-Item -LiteralPath $VenvPath -Recurse -Force
    }

    Write-Step "Creating venv and installing locked dependencies via uv..." -Level cmd
    $uvArgs = Build-UvSyncArgs -ProjectRoot $ProjectRoot -Dev:$Dev -Minimal:$Minimal -Extras $Extras
    Invoke-Checked -Exe "uv" -Arguments $uvArgs -ErrorMessage "uv sync failed."
    Write-Step "Virtual environment created successfully (uv, locked)." -Level ok

    $activatePath = Join-Path $VenvPath "Scripts\Activate.ps1"
    Write-Step "  Activate: . $activatePath"
}
catch {
    Write-Step "create_venv failed: $($_.Exception.Message)" -Level err
    exit 1
}
