# ------------------------------------------------------------
# Shared helpers for project PowerShell scripts.
#
# Dot-source this file at the top of any script:
#   . (Join-Path $PSScriptRoot "_common.ps1")
#
# Provides:
#   Resolve-ProjectRoot   - walk-up search for repo markers
#   Get-PythonRunner      - locate best python (venv > py > python)
#   Get-PythonVersion     - return [version] from a runner
#   Assert-IsoDate        - validate YYYY-MM-DD strings
#   Write-Step            - consistent coloured log lines
#   Invoke-Checked        - run an external command and throw on failure
#   Format-RunnerLabel    - human-readable label for a runner hashtable
#   Assert-UvAvailable    - throw with install guidance when uv is missing
#   Build-UvSyncArgs      - assemble the `uv sync` argument array
#   Invoke-DriverSelfHeal - ensure a Python module is importable, install its extra
# ------------------------------------------------------------

Set-StrictMode -Version Latest

function Resolve-ProjectRoot {
    <#
    .SYNOPSIS
        Walk upward from StartDir looking for repo/project markers.
        Falls back to the parent of StartDir (the /scripts to repo-root convention).
    #>
    param(
        [Parameter(Mandatory)]
        [string]$StartDir,

        [string[]]$ExtraMarkers = @()
    )

    $markers = @(".git", "pyproject.toml", "uv.lock") + $ExtraMarkers
    $dir = (Resolve-Path $StartDir).Path

    while ($true) {
        foreach ($m in $markers) {
            if (Test-Path -LiteralPath (Join-Path $dir $m)) {
                return $dir
            }
        }

        $parent = Split-Path $dir -Parent
        if ([string]::IsNullOrWhiteSpace($parent) -or $parent -eq $dir) { break }
        $dir = $parent
    }

    return (Resolve-Path (Join-Path $StartDir "..")).Path
}

function Get-PythonRunner {
    <#
    .SYNOPSIS
        Return a hashtable @{ Cmd; Args } for the best available Python.
        Search order: venv python > py launcher (targeted, then generic) > bare python.
    #>
    param(
        [version]$MinVersion = "3.11",
        [string]$VenvPath
    )

    # 1. Venv python (highest priority when a venv exists).
    if ($VenvPath) {
        $venvPy = Join-Path $VenvPath "Scripts\python.exe"
        if (Test-Path -LiteralPath $venvPy) {
            return @{ Cmd = $venvPy; Args = @() }
        }
    }

    # 2. Windows Python Launcher (targeted minor, then generic -3).
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $candidates = @(
            @{ Cmd = "py"; Args = @("-$($MinVersion.Major).$($MinVersion.Minor)") },
            @{ Cmd = "py"; Args = @("-3") }
        )

        foreach ($cand in $candidates) {
            try {
                $null = & $cand.Cmd @($cand.Args) -c "import sys" 2>$null
                $ec = $LASTEXITCODE
                if ($ec -eq 0) { return $cand }
            } catch { }
        }
    }

    # 3. Bare python on PATH.
    if (Get-Command python -ErrorAction SilentlyContinue) {
        return @{ Cmd = "python"; Args = @() }
    }

    return $null
}

function Get-PythonVersion {
    <#
    .SYNOPSIS  Return [version] for a runner returned by Get-PythonRunner.
    #>
    param(
        [Parameter(Mandatory)]
        [hashtable]$Runner
    )

    $code = 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")'
    $out = & $Runner.Cmd @($Runner.Args) -c $code 2>$null
    $ec = $LASTEXITCODE

    if ($ec -ne 0 -or -not $out) {
        throw "Python was found ($($Runner.Cmd)) but could not report its version."
    }

    $trimmed = $out.Trim()
    try {
        return [version]$trimmed
    } catch {
        throw "Python reported an unparseable version string: '$trimmed'."
    }
}

function Assert-IsoDate {
    <#
    .SYNOPSIS  Validate that a string is YYYY-MM-DD (or empty/null).
    #>
    param(
        [string]$Value,
        [string]$Name = "Date"
    )

    if ([string]::IsNullOrWhiteSpace($Value)) { return }
    try {
        [void][DateTime]::ParseExact($Value, "yyyy-MM-dd", $null)
    } catch {
        throw "$Name must be YYYY-MM-DD. Got: '$Value'."
    }
}

function Write-Step {
    <#
    .SYNOPSIS  Emit a consistently formatted status line.
    .PARAMETER Level
        info (default, DarkGray), ok (Green), warn (Yellow), err (Red), cmd (Cyan).
    #>
    param(
        [Parameter(Mandatory)]
        [string]$Message,

        [ValidateSet("info","ok","warn","err","cmd")]
        [string]$Level = "info"
    )

    $color = switch ($Level) {
        "info" { "DarkGray" }
        "ok"   { "Green"    }
        "warn" { "Yellow"   }
        "err"  { "Red"      }
        "cmd"  { "Cyan"     }
    }

    Write-Host $Message -ForegroundColor $color
}

function Invoke-Checked {
    <#
    .SYNOPSIS  Run an external command; throw when it returns a non-zero exit code.
    #>
    param(
        [Parameter(Mandatory)]
        [string]$Exe,

        [string[]]$Arguments = @(),

        [string]$ErrorMessage
    )

    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) {
        $msg = if ($ErrorMessage) { $ErrorMessage } else { "$Exe exited with code $LASTEXITCODE." }
        throw $msg
    }
}

function Format-RunnerLabel {
    <#
    .SYNOPSIS  Human-readable label for a python runner hashtable.
    #>
    param([hashtable]$Runner)

    if ($Runner.Args -and $Runner.Args.Count -gt 0) {
        return "$($Runner.Cmd) $($Runner.Args -join ' ')"
    }
    return $Runner.Cmd
}

function Assert-UvAvailable {
    <#
    .SYNOPSIS
        Throw with copy-paste install guidance when uv is not on PATH.
        This project standardizes on uv for locked, reproducible installs.
    #>
    if (Get-Command uv -ErrorAction SilentlyContinue) { return }
    throw ("uv is required but was not found on PATH. Install it, then re-run:`n" +
           "  - with Python:  pip install uv`n" +
           "  - without Python (Windows):  irm https://astral.sh/uv/install.ps1 | iex`n" +
           "  See https://docs.astral.sh/uv/ for other platforms.")
}

function Build-UvSyncArgs {
    <#
    .SYNOPSIS
        Assemble the argument array for `uv sync`, shared by create_venv/sync_venv.
        Extras are appended unless -Minimal is set.
    #>
    param(
        [Parameter(Mandatory)][string]$ProjectRoot,
        [switch]$Dev,
        [switch]$Minimal,
        [string[]]$Extras = @(),
        [switch]$Quiet
    )

    $uvArgs = @("sync", "--project", $ProjectRoot)
    $uvArgs += if ($Dev) { "--dev" } else { "--no-dev" }
    if (-not $Minimal) {
        foreach ($x in $Extras) {
            if (-not [string]::IsNullOrWhiteSpace($x)) { $uvArgs += @("--extra", $x) }
        }
    }
    if ($Quiet) { $uvArgs += "--quiet" }
    return ,$uvArgs
}

function Invoke-DriverSelfHeal {
    <#
    .SYNOPSIS
        Ensure $Module is importable in $PythonExe; if not, install its uv extra
        (uv sync --extra <Extra>) and re-check. Throws if it still can't be
        imported. Optional DB-import drivers (pyodbc/psycopg) live in extras that
        a bare `uv sync` prunes, so the import runners self-heal here rather than
        failing deep inside the Python import.
    #>
    param(
        [Parameter(Mandatory)][string]$PythonExe,
        [Parameter(Mandatory)][string]$Module,
        [Parameter(Mandatory)][string]$Extra,
        [Parameter(Mandatory)][string]$ProjectRoot
    )

    & $PythonExe -c "import $Module" 2>$null
    if ($LASTEXITCODE -eq 0) { return }

    Write-Step "$Module not found in this interpreter - auto-installing the '$Extra' extra via uv..." -Level warn
    Assert-UvAvailable
    & uv sync --project $ProjectRoot --extra $Extra 2>&1 | Out-Null
    & $PythonExe -c "import $Module" 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "$Module is required but could not be installed automatically. Run:  uv sync --extra $Extra"
    }
    Write-Step "$Module installed successfully." -Level ok
}