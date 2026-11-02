<#
.SYNOPSIS
  Builds the Claude Session Browser installer (developer machines only).

.DESCRIPTION
  Uses the project's own Python environment (.venv, Python 3.12) created by the
  dev setup (Clawdmeter\dev-setup.ps1). Steps:
    1. translation check (tools\check_i18n.py)
    2. tests (pytest)
    3. the app, as a PyInstaller "onedir" build
    4. the separate updater (csb_updater.exe)
    5. the installer (Inno Setup) -> dist\ClaudeSessionBrowser-Setup.exe
  The version comes from VERSION in claude_sessions.py.

  End users never run this. They install the released installer (install.ps1).

.EXAMPLE
  .\build.ps1
  .\build.ps1 -SkipTests
#>
param([switch]$SkipTests)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$env:PYTHONIOENCODING = 'utf-8'

function Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }
function Check($what) { if ($LASTEXITCODE -ne 0) { throw "$what failed (exit $LASTEXITCODE)." } }

$py = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) {
    throw "No .venv here. Run the dev setup first (Clawdmeter\dev-setup.ps1)."
}
$iscc = @(
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) {
    throw "Inno Setup 6 not found. Install it: winget install JRSoftware.InnoSetup --scope user"
}
$version = (Select-String -Path claude_sessions.py -Pattern '^VERSION = "([^"]+)"').Matches[0].Groups[1].Value
Write-Host "Claude Session Browser $version"

Step "1/5 Translations"
& $py tools\check_i18n.py | Select-Object -Last 1
Check "Translation check"

if (-not $SkipTests) {
    Step "2/5 Tests"
    & $py -m pytest -q tests
    Check "Tests"
} else {
    Step "2/5 Tests skipped"
}

Remove-Item build, dist -Recurse -Force -ErrorAction SilentlyContinue

Step "3/5 App (PyInstaller, onedir)"
& $py -m PyInstaller --noconfirm --clean --log-level WARN `
    --onedir --noconsole `
    --name ClaudeSessionBrowser `
    --icon claude_sessions.ico `
    --add-data "claude_sessions.ico;." `
    --add-data "hooks\csb-name-gate.ps1;hooks" `
    --hidden-import pystray --hidden-import PIL --hidden-import accounts --hidden-import sessions_live --hidden-import sysstats --hidden-import termfocus `
    --hidden-import psutil --hidden-import pynvml --hidden-import clr `
    claude_sessions.py
Check "PyInstaller (app)"

Step "4/5 Updater (csb_updater.exe)"
& $py -m PyInstaller --noconfirm --clean --log-level WARN `
    --onefile --noconsole --name csb_updater --icon claude_sessions.ico `
    csb_updater.py
Check "PyInstaller (updater)"

Step "5/5 Installer (Inno Setup)"
& $iscc /Q "/DMyAppVersion=$version" setup.iss
Check "Inno Setup"

$setup = Join-Path $PSScriptRoot 'dist\ClaudeSessionBrowser-Setup.exe'
$hash = (Get-FileHash $setup -Algorithm SHA256).Hash.ToLower()
$size = [math]::Round((Get-Item $setup).Length / 1MB, 1)
Write-Host "`nDone: $setup ($size MB)" -ForegroundColor Green
Write-Host "SHA-256: $hash"
