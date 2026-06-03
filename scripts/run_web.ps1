<#
.SYNOPSIS
    Launch the FastAPI + React web UI for the Synthetic Data Generator.

.DESCRIPTION
    Starts uvicorn serving web.api:app on port 8502.
    Automatically installs fastapi and uvicorn if missing.

.PARAMETER Reload
    Pass -Reload to enable auto-reload during development.

.EXAMPLE
    .\scripts\run_web.ps1
    .\scripts\run_web.ps1 -Reload
#>
param(
    [switch]$Reload,
    [int]$Port = 8502,
    [string]$Host_ = "127.0.0.1"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Resolve repo root (script lives in <repo>/scripts/)
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Definition
$RepoRoot   = (Resolve-Path (Join-Path $ScriptDir "..")).Path

. (Join-Path $ScriptDir "_common.ps1")

Push-Location $RepoRoot
try {
    # Activate venv if present
    $VenvActivate = Join-Path $RepoRoot ".venv\Scripts\Activate.ps1"
    if (Test-Path $VenvActivate) {
        Write-Host "Activating virtual environment..." -ForegroundColor DarkGray
        & $VenvActivate
    }

    # Ensure web dependencies are present; restore from the lockfile if not.
    python -c "import fastapi, uvicorn" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Web dependencies missing. Restoring with uv sync..." -ForegroundColor Yellow
        Assert-UvAvailable
        uv sync --project $RepoRoot
    }

    # Check port availability before launching
    try {
        $listener = [System.Net.Sockets.TcpClient]::new()
        $listener.Connect($Host_, $Port)
        $listener.Close()
        Write-Host "Port $Port is already in use. Choose a different port with -Port." -ForegroundColor Red
        exit 1
    } catch [System.Net.Sockets.SocketException] {
        # Port is available (connection refused = nobody listening)
    }

    # Launch
    $Url = "http://${Host_}:${Port}"
    $UvicornArgs = @("--host", $Host_, "--port", $Port)
    if ($Reload) { $UvicornArgs += "--reload" }

    Write-Host ""
    Write-Host "Starting web UI at $Url" -ForegroundColor Cyan
    Write-Host "Press Ctrl+C to stop." -ForegroundColor DarkGray
    Write-Host ""

    # Open browser after a short delay (non-blocking)
    Start-Job -ScriptBlock {
        param($u)
        Start-Sleep -Milliseconds 1500
        Start-Process $u
    } -ArgumentList $Url | Out-Null

    python -m uvicorn web.api:app @UvicornArgs
}
finally {
    Pop-Location
}
