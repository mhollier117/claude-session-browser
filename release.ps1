<#
.SYNOPSIS
  Makes a release of the Claude Session Browser (developer machines only).

.DESCRIPTION
  1. Sets VERSION in claude_sessions.py
  2. Builds the installer (build.ps1: translations, tests, PyInstaller, Inno Setup)
  3. Writes version.json: version, notes, release URLs pinned to the tag, and
     the installer's SHA-256 (install.ps1 and the in-app updater check it)
  4. Commits both files
  5. Only with -Publish: pushes and creates the GitHub release with the installer
     and a portable zip of the app folder (both with SHA-256 in the notes)

  Version scheme: upstream version + a 4th number (1.6.1.1, 1.6.1.2, ...).
  On moving to a newer upstream release, start at e.g. 1.6.2.1.

.EXAMPLE
  .\release.ps1 -Version 1.6.1.1 -Notes "Accounts page"            # build + commit only
  .\release.ps1 -Version 1.6.1.1 -Notes "Accounts page" -Publish   # and publish
#>
param(
    [Parameter(Mandatory)][ValidatePattern('^\d+\.\d+\.\d+(\.\d+)?$')][string]$Version,
    [string]$Notes = "",
    [switch]$Publish
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$repo = 'mhollier117/claude-session-browser'

if (git status --porcelain --untracked-files=no | Where-Object { $_ -notmatch '\.claude-project$' }) {
    throw "Uncommitted changes. Commit or stash them first."
}
if ((git rev-parse --abbrev-ref HEAD) -ne 'main') { throw "Releases are made from main." }

# 1. Version into the app
$src = Get-Content claude_sessions.py -Raw -Encoding utf8
$src = [regex]::Replace($src, '(?m)^VERSION = "[^"]+"', "VERSION = `"$Version`"")
[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'claude_sessions.py'), $src, (New-Object Text.UTF8Encoding $false))

# 2. Build
& (Join-Path $PSScriptRoot 'build.ps1')
$setup = Join-Path $PSScriptRoot 'dist\ClaudeSessionBrowser-Setup.exe'
$hash = (Get-FileHash $setup -Algorithm SHA256).Hash.ToLower()

# Portable zip: the same onedir app folder the installer ships, for running
# without installing (unzip anywhere, start ClaudeSessionBrowser.exe).
$portable = Join-Path $PSScriptRoot "dist\ClaudeSessionBrowser-$Version-portable.zip"
Remove-Item $portable -ErrorAction SilentlyContinue
Compress-Archive -Path (Join-Path $PSScriptRoot 'dist\ClaudeSessionBrowser') -DestinationPath $portable
$phash = (Get-FileHash $portable -Algorithm SHA256).Hash.ToLower()

# 3. version.json (download pinned to this tag, so the checksum always matches)
$tag = "v$Version"
$manifest = [ordered]@{
    version          = $Version
    notes            = $Notes
    url              = "https://github.com/$repo/releases/tag/$tag"
    installer_url    = "https://github.com/$repo/releases/download/$tag/ClaudeSessionBrowser-Setup.exe"
    installer_sha256 = $hash
}
$json = ($manifest | ConvertTo-Json) + "`n"
[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'version.json'), $json, (New-Object Text.UTF8Encoding $false))

# 4. Commit
git add claude_sessions.py version.json
git commit -q -m "Release $Version`n`n$Notes"
Write-Host "Committed release $Version (installer SHA-256 $hash)." -ForegroundColor Green

# 5. Publish
if ($Publish) {
    git push -q origin main
    $body = "$Notes`n`n**Downloads**`n- ClaudeSessionBrowser-Setup.exe: installer (per user, no admin), keeps itself updated.`n- ClaudeSessionBrowser-$Version-portable.zip: the same app without installing. Unzip anywhere and start ClaudeSessionBrowser.exe.`n`nSHA-256`n- Setup: ``$hash```n- Portable: ``$phash``"
    gh release create $tag $setup $portable --repo $repo --title "Claude Session Browser $Version" --notes $body
    Write-Host "Published $tag." -ForegroundColor Green
} else {
    Write-Host "Not published. To publish: git push origin main; gh release create $tag `"$setup`" --repo $repo --notes `"...`""
}
