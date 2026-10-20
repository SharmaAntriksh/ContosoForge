"""Exercise native stderr handling in both Windows PowerShell and PowerShell 7."""

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.mark.parametrize("shell", ["powershell.exe", "pwsh"])
@pytest.mark.parametrize("scenario", ["present", "install", "install_failed", "still_missing"])
def test_driver_self_heal(shell, scenario, tmp_path):
    executable = shutil.which(shell)
    if executable is None:
        pytest.skip(f"{shell} unavailable")

    module = "contosoforge_test_optional_driver"
    if scenario == "present":
        (tmp_path / f"{module}.py").write_text("", encoding="utf-8")
    common = Path(__file__).resolve().parents[1] / "scripts" / "_common.ps1"
    script = tmp_path / "preflight.ps1"
    script.write_text(
        """
param($Common, $PythonExe, $ProjectRoot, $Scenario, $Module)
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true
. $Common
$script:installCalls = 0
function uv {
    $script:installCalls += 1
    # uv writes normal progress to stderr, even on success.
    & $PythonExe -c "import sys; sys.stderr.write('installer progress\\n')" 2>&1
    if ($Scenario -eq 'install_failed') {
        $global:LASTEXITCODE = 7
        return
    }
    if ($Scenario -eq 'install') {
        Set-Content -LiteralPath (Join-Path $ProjectRoot "$Module.py") -Value ''
    }
    $global:LASTEXITCODE = 0
}
try {
    Invoke-DriverSelfHeal -PythonExe $PythonExe -Module $Module -Extra sql -ProjectRoot $ProjectRoot
    if ($Scenario -in @('install_failed', 'still_missing')) { throw 'Expected failure' }
    $expectedCalls = if ($Scenario -eq 'present') { 0 } else { 1 }
    if ($script:installCalls -ne $expectedCalls) { throw 'Incorrect install count' }
} catch {
    $message = $_.Exception.Message
    if ($Scenario -eq 'install_failed' -and $message -like '*exit code 7*installer progress*') {
        exit 0
    }
    if ($Scenario -eq 'still_missing' -and $message -like '*required but could not be installed*') {
        exit 0
    }
    Write-Output $message
    exit 1
}
if ($ErrorActionPreference -ne 'Stop') { throw 'Caller preference changed' }
if (-not $PSNativeCommandUseErrorActionPreference) { throw 'Caller native preference changed' }
exit 0
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [executable, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), str(common), sys.executable,
         str(tmp_path), scenario, module],
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
