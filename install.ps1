<#
.SYNOPSIS
  Installs or updates the Claude Session Browser (with multi-account support)
  on any Windows PC. Nothing is built; it uses the released installer.

.DESCRIPTION
  1. Reads version.json from github.com/mhollier117/claude-session-browser
  2. Downloads the released installer it points to
  3. Refuses to run it unless its SHA-256 matches the published checksum
  4. Runs the installer (per-user, no admin needed)

  One-liner (PowerShell):
    irm https://raw.githubusercontent.com/mhollier117/claude-session-browser/main/install.ps1 | iex

  From a downloaded copy of this script:
    .\install.ps1               # latest release
    .\install.ps1 -Silent       # no installer wizard
    .\install.ps1 -Setup C:\path\ClaudeSessionBrowser-Setup.exe   # a local installer
#>
param(
    [string]$Setup,
    [switch]$Silent
)

$ErrorActionPreference = 'Stop'
$repo = 'mhollier117/claude-session-browser'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

if (-not $Setup) {
    Write-Host "Checking the latest release of $repo ..."
    $manifest = Invoke-RestMethod "https://raw.githubusercontent.com/$repo/main/version.json"
    if (-not $manifest.installer_url -or -not $manifest.installer_sha256) {
        throw "No release with a checksum is published yet. Nothing was installed."
    }
    $Setup = Join-Path $env:TEMP "ClaudeSessionBrowser-Setup-$($manifest.version).exe"
    Write-Host "Downloading version $($manifest.version) ..."
    Invoke-WebRequest $manifest.installer_url -OutFile $Setup -UseBasicParsing
    $hash = (Get-FileHash $Setup -Algorithm SHA256).Hash.ToLower()
    if ($hash -ne $manifest.installer_sha256.ToLower()) {
        Remove-Item $Setup -Force
        throw "Checksum mismatch: the download isn't the published installer. Nothing was installed."
    }
    Write-Host "Checksum OK."
} elseif (-not (Test-Path $Setup)) {
    throw "Installer not found: $Setup"
}

Write-Host "Running the installer ..."
$installerArgs = if ($Silent) { @('/SILENT', '/NORESTART') } else { @() }
$p = Start-Process -FilePath $Setup -ArgumentList $installerArgs -Wait -PassThru
if ($p.ExitCode -ne 0) { throw "The installer exited with code $($p.ExitCode)." }

Write-Host ""
Write-Host "Installed. Next steps:" -ForegroundColor Green
Write-Host "  1. Pair the Clawdmeter: Settings > Bluetooth & devices > Add device > Bluetooth > 'Clawdmeter'."
Write-Host "     If it doesn't show up (or it was paired to another PC), hold its PWR button ~3 s and release."
Write-Host "  2. In the Session Browser: Clawdmeter tab > switch on 'Connection active' > pick the device."
Write-Host "  3. Accounts tab: your signed-in Claude account appears on its own. Use 'Add account' for more."
