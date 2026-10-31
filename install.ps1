#Requires -Version 5.1
<#
.SYNOPSIS
    One-click installer for the APIPaw Agent (Windows).

.DESCRIPTION
    Creates a local virtual environment, installs Python dependencies and
    generates agent.config.json with sensible defaults.

    NOTE: This script intentionally contains ASCII-only text. PowerShell 5.1
    reads .ps1 files using the system ANSI code page unless a UTF-8 BOM is
    present, so non-ASCII characters could be mis-decoded on GBK systems.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install.ps1 -NoVenv -StartOnBoot
#>

[CmdletBinding()]
param(
    [switch]$NoVenv,
    [switch]$StartOnBoot,
    [switch]$SkipConfig,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

function Write-Step { param([string]$Message) Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Ok   { param([string]$Message) Write-Host "    [OK] $Message" -ForegroundColor Green }
function Write-Warn { param([string]$Message) Write-Host "    [WARN] $Message" -ForegroundColor Yellow }
function Write-Err  { param([string]$Message) Write-Host "    [ERROR] $Message" -ForegroundColor Red }

# ---------------------------------------------------------------------------
# 1. Locate a usable Python interpreter (3.10+)
# ---------------------------------------------------------------------------
Write-Step "Checking Python"

function Get-PythonCommand {
    param([string]$Preferred)

    $candidates = @()
    if ($Preferred) { $candidates += $Preferred }
    if (Get-Command "py" -ErrorAction SilentlyContinue) { $candidates += "py" }
    if (Get-Command "python" -ErrorAction SilentlyContinue) { $candidates += "python" }
    if (Get-Command "python3" -ErrorAction SilentlyContinue) { $candidates += "python3" }

    foreach ($candidate in $candidates) {
        try {
            $version = & $candidate -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and $version) {
                $parts = $version.Trim().Split(".")
                if ([int]$parts[0] -ge 3 -and [int]$parts[1] -ge 10) {
                    return $candidate
                }
            }
        } catch {
            continue
        }
    }
    return $null
}

$pythonCmd = Get-PythonCommand -Preferred $Python
if (-not $pythonCmd) {
    Write-Err "Python 3.10+ was not found on PATH."
    Write-Host "    Install it from https://www.python.org/downloads/ and re-run this script." -ForegroundColor Red
    exit 1
}
Write-Ok "Using '$pythonCmd' ($(& $pythonCmd -c "import sys; print(sys.version.split()[0])"))"

# ---------------------------------------------------------------------------
# 2. Virtual environment
# ---------------------------------------------------------------------------
$VenvDir = Join-Path $ProjectRoot ".venv"
$PythonBin = Join-Path $VenvDir "Scripts\python.exe"

if ($NoVenv) {
    Write-Warn "Skipping virtual environment (--NoVenv). Dependencies will be installed into the current interpreter."
    $PythonBin = $pythonCmd
} elseif (Test-Path $PythonBin) {
    Write-Step "Reusing existing virtual environment at .venv"
    Write-Ok $VenvDir
} else {
    Write-Step "Creating virtual environment at .venv"
    & $pythonCmd -m venv $VenvDir
    if (-not (Test-Path $PythonBin)) {
        Write-Err "Failed to create the virtual environment."
        exit 1
    }
    Write-Ok "Virtual environment created"
}

# ---------------------------------------------------------------------------
# 3. Dependencies
# ---------------------------------------------------------------------------
Write-Step "Installing Python dependencies"
& $PythonBin -m pip install --upgrade pip --quiet
& $PythonBin -m pip install -r (Join-Path $ProjectRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    Write-Err "Dependency installation failed."
    exit 1
}
Write-Ok "Dependencies installed"

# ---------------------------------------------------------------------------
# 4. agent.config.json
# ---------------------------------------------------------------------------
$ConfigPath = Join-Path $ProjectRoot "agent.config.json"
if ($SkipConfig) {
    Write-Warn "Skipping configuration generation (--SkipConfig)."
} elseif (Test-Path $ConfigPath) {
    Write-Step "Configuration already exists, keeping it"
    Write-Ok "agent.config.json"
} else {
    Write-Step "Generating agent.config.json"
    & $PythonBin -c "from agent_config import get_config; get_config()"
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "Could not generate the configuration automatically; create agent.config.json manually."
    } else {
        Write-Ok "agent.config.json created"
    }
}

# ---------------------------------------------------------------------------
# 5. Optional: autostart on login
# ---------------------------------------------------------------------------
if ($StartOnBoot) {
    Write-Step "Registering autostart shortcut"
    $StartupDir = [Environment]::GetFolderPath("Startup")
    $ShortcutPath = Join-Path $StartupDir "APIPaw-Agent.lnk"
    $Target = Join-Path $VenvDir "Scripts\pythonw.exe"
    $Arguments = "`"$ProjectRoot\main.py`""
    try {
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($ShortcutPath)
        $shortcut.TargetPath = $Target
        $shortcut.Arguments = $Arguments
        $shortcut.WorkingDirectory = $ProjectRoot
        $shortcut.WindowStyle = 7
        $shortcut.Save()
        Write-Ok "Autostart shortcut: $ShortcutPath"
    } catch {
        Write-Warn "Could not create the autostart shortcut: $($_.Exception.Message)"
    }
}

# ---------------------------------------------------------------------------
# 6. Summary
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "APIPaw Agent installation finished." -ForegroundColor Green
Write-Host ""
Write-Host "Start the agent:" -ForegroundColor Cyan
Write-Host "    .\.venv\Scripts\python.exe main.py"
Write-Host ""
Write-Host "Health check:" -ForegroundColor Cyan
Write-Host "    curl http://127.0.0.1:52415/health"
Write-Host ""
Write-Host "Edit 'agent.config.json' to set node_name, hardware_type, wallet_address and node_secret." -ForegroundColor Cyan
exit 0
