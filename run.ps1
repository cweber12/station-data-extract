# run.ps1 -- launcher for the La Jolla sensor comparison tool.
#
# Right-click this file and choose "Run with PowerShell", or from a prompt:
#     cd <this folder>
#     .\run.ps1
#
# If PowerShell blocks the script, run this once in the same window:
#     Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

Write-Host 'La Jolla sensor comparison' -ForegroundColor Cyan
Write-Host ''

# --- find a usable Python -------------------------------------------------
$py = $null
foreach ($candidate in @('py -3', 'python', 'python3')) {
    $parts = $candidate.Split(' ')
    $exe = $parts[0]
    if (Get-Command $exe -ErrorAction SilentlyContinue) {
        try {
            $v = & $exe $parts[1..($parts.Length - 1)] -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>$null
            if ($LASTEXITCODE -eq 0 -and $v) {
                $maj, $min = $v.Trim().Split('.')
                if ([int]$maj -ge 3 -and [int]$min -ge 9) {
                    $py = $candidate
                    Write-Host "Python $($v.Trim()) found ($candidate)" -ForegroundColor Green
                    break
                }
            }
        } catch { }
    }
}

if (-not $py) {
    Write-Host 'No Python 3.9 or newer found on PATH.' -ForegroundColor Red
    Write-Host 'Install from https://www.python.org/downloads/ and tick'
    Write-Host '"Add python.exe to PATH" during setup, then run this again.'
    Read-Host 'Press Enter to close'
    exit 1
}

$pyParts = $py.Split(' ')
$pyExe = $pyParts[0]
$pyArgs = if ($pyParts.Length -gt 1) { $pyParts[1..($pyParts.Length - 1)] } else { @() }

function Invoke-Py { & $pyExe @pyArgs @args }

# --- tkinter ---------------------------------------------------------------
Invoke-Py -c 'import tkinter' 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host 'tkinter is missing from this Python install.' -ForegroundColor Red
    Write-Host 'Re-run the Python installer, choose Modify, and enable'
    Write-Host '"tcl/tk and IDLE". On Linux: sudo apt install python3-tk'
    Read-Host 'Press Enter to close'
    exit 1
}

# --- dependencies ----------------------------------------------------------
$missing = @()
foreach ($m in @('pandas', 'openpyxl', 'numpy')) {
    Invoke-Py -c "import $m" 2>$null
    if ($LASTEXITCODE -ne 0) { $missing += $m }
}

if ($missing.Count -gt 0) {
    Write-Host "Installing: $($missing -join ', ')" -ForegroundColor Yellow
    Invoke-Py -m pip install --user --disable-pip-version-check @missing
    if ($LASTEXITCODE -ne 0) {
        Write-Host ''
        Write-Host 'Install failed. If this machine blocks PyPI, ask IT for a' -ForegroundColor Red
        Write-Host 'local index and use:  pip install --index-url <url> pandas openpyxl numpy'
        Read-Host 'Press Enter to close'
        exit 1
    }
}

# --- folders ---------------------------------------------------------------
foreach ($d in @('sources', 'outputs')) {
    if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d | Out-Null }
}

$n = @(Get-ChildItem -Path 'sources' -Filter '*.xlsx' -ErrorAction SilentlyContinue).Count
if ($n -eq 0) {
    Write-Host ''
    Write-Host 'No .xlsx files in sources\ -- put your workbooks there first.' -ForegroundColor Yellow
}

Write-Host ''
Write-Host 'Launching...' -ForegroundColor Cyan
Invoke-Py compare.py

if ($LASTEXITCODE -ne 0) {
    Write-Host ''
    Write-Host 'The app exited with an error (above).' -ForegroundColor Red
    Read-Host 'Press Enter to close'
}
