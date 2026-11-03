# csb-name-gate.ps1 - Claude Session Browser naming gate (UserPromptSubmit hook).
#
# Blocks a prompt in an unnamed Claude Code terminal session until the session
# has a custom name (/rename <name>, or claude -n <name>), so the Clawdmeter and
# the Session Browser can show which session is which.
#
# FAIL-OPEN: any error, missing data or unexpected input lets the prompt through.
# It only ever blocks when ALL of these hold:
#   - the event is UserPromptSubmit and the hook input has no session_title
#     (Claude Code only sends it for a custom name; auto titles don't count)
#   - the prompt is not a slash command (/rename itself must get through), a
#     ! shell command, or a message from another Claude session
#   - a "source" field, if Claude Code ever sends one, says "user"
#   - this session's entry in Claude Code's session registry
#     (<config>\sessions\*.json) says entrypoint "cli", i.e. an interactive
#     terminal. Print mode / SDK ("sdk-cli"), IDE and desktop sessions pass.
#
# Off switches: environment variable CSB_NAME_GATE=0, or the file
# %USERPROFILE%\.claude\name-gate.off (or <CLAUDE_CONFIG_DIR>\name-gate.off).
#
# Installed to %USERPROFILE%\.claude\hooks\ by the Session Browser. Windows
# PowerShell 5.1 compatible. Never writes anything.

$ErrorActionPreference = 'Stop'
try {
    if ($env:CSB_NAME_GATE -eq '0') { exit 0 }

    $cfg = $env:CLAUDE_CONFIG_DIR
    if (-not $cfg) { $cfg = Join-Path $env:USERPROFILE '.claude' }
    if (Test-Path -LiteralPath (Join-Path $cfg 'name-gate.off')) { exit 0 }
    $homeOff = Join-Path $env:USERPROFILE '.claude\name-gate.off'
    if (Test-Path -LiteralPath $homeOff) { exit 0 }

    # Read stdin as UTF-8 regardless of the console code page.
    $reader = New-Object System.IO.StreamReader([Console]::OpenStandardInput(), [Text.Encoding]::UTF8)
    $raw = $reader.ReadToEnd()
    if (-not $raw -or -not $raw.Trim()) { exit 0 }
    $d = $raw | ConvertFrom-Json

    if ($d.hook_event_name -and $d.hook_event_name -ne 'UserPromptSubmit') { exit 0 }
    if ($d.session_title -and ([string]$d.session_title).Trim()) { exit 0 }
    if ($d.source -and $d.source -ne 'user') { exit 0 }

    $prompt = ([string]$d.prompt).TrimStart()
    if ($prompt.StartsWith('/') -or $prompt.StartsWith('!')) { exit 0 }
    # A message from another Claude session (SendMessage) also arrives as
    # UserPromptSubmit; the user didn't type it, and blocking only bounces it.
    if ($prompt.StartsWith('<cross-session-message')) { exit 0 }

    $sid = [string]$d.session_id
    if (-not $sid) { exit 0 }

    # Find this session's registry entry. Try the PID Claude Code exports
    # first, then scan (a handful of small files).
    $sessDir = Join-Path $cfg 'sessions'
    $entry = $null
    if ($env:CLAUDE_PID -match '^\d+$') {
        $f = Join-Path $sessDir ($env:CLAUDE_PID + '.json')
        if (Test-Path -LiteralPath $f) {
            $e = [IO.File]::ReadAllText($f) | ConvertFrom-Json
            if ($e.sessionId -eq $sid) { $entry = $e }
        }
    }
    if (-not $entry -and (Test-Path -LiteralPath $sessDir)) {
        foreach ($f in [IO.Directory]::GetFiles($sessDir, '*.json')) {
            try {
                $e = [IO.File]::ReadAllText($f) | ConvertFrom-Json
                if ($e.sessionId -eq $sid) { $entry = $e; break }
            } catch { }
        }
    }
    if (-not $entry) { exit 0 }
    if ($entry.entrypoint -ne 'cli') { exit 0 }
    if ($entry.kind -and $entry.kind -ne 'interactive') { exit 0 }

    [Console]::Error.WriteLine('Name this session first: /rename <name>  (the Clawdmeter shows sessions by name). Then send your message again. To turn this off: Session Browser > Clawdmeter > Naming gate, or create ~/.claude/name-gate.off.')
    exit 2
} catch {
    exit 0
}
