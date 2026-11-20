# cc: resume or start a Claude Code / Qwen session in this tab.
# Dot-source from your PowerShell profile:   . C:\path\to\claude-session-browser\tools\cc.ps1
#
# The launcher draws its screens on the console and prints exactly one JSON line on stdout when a launch is
# chosen: {"cd": "<folder>", "run": ["<wrapper>", "<arg>", ...], "warnings": [...]}. This function reads that
# line, changes to the folder and runs the wrapper here, so the session starts in the tab you are already in.
# Anything else on stdout (cc --json, cc --help) is passed through unchanged.

function cc {
    # Windows PowerShell 5.1 mangles double quotes in arguments passed to native programs, so a session name with
    # a `"` would break: hand over to PowerShell 7, whose profile defines cc. The arguments travel as single-quoted
    # literals inside a base64 -EncodedCommand, so no double quote reaches the command line. Not a scriptblock
    # (`pwsh -Command { ... } -args ...`): for that form 5.1 adds `-inputFormat xml` and pipes the child's stdin and
    # output, and cc's screens need the console. pwsh sets $LASTEXITCODE, which is what cc returns.
    if ($PSVersionTable.PSVersion.Major -lt 7) {
        if (Get-Command pwsh -CommandType Application -ErrorAction SilentlyContinue) {
            # each single quote doubled, the typographic ones too (PowerShell reads them as quotes); ASCII source,
            # since 5.1 reads a script without a BOM in the ANSI code page
            $sq = "['" + [char]0x2018 + [char]0x2019 + [char]0x201A + [char]0x201B + "]"
            $quoted = @($args | ForEach-Object { "'" + ([string]$_ -replace $sq, '$0$0') + "'" })
            $text = (@('cc') + $quoted) -join ' '
            pwsh -NoLogo -EncodedCommand ([Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($text)))
            return
        }
        Write-Host 'cc needs PowerShell 7 (pwsh) for session names with double quotes; continuing in Windows PowerShell.'
    }
    $repo = Split-Path -Parent $PSScriptRoot
    # the installed onedir build ({app}\cc\cc.exe), then a cc.exe beside this script (older single-file layout)
    $exe = @((Join-Path $PSScriptRoot 'cc\cc.exe'), (Join-Path $PSScriptRoot 'cc.exe')) |
        Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    $venvPy = Join-Path $repo '.venv\Scripts\python.exe'
    if ($exe) {
        $cmd = $exe; $pre = @()
    } elseif (Test-Path -LiteralPath $venvPy) {
        $cmd = $venvPy; $pre = @('-m', 'launcher.cli')
    } else {
        $cmd = 'python'; $pre = @('-m', 'launcher.cli')
    }

    $prevOut = [Console]::OutputEncoding
    $prevPath = $env:PYTHONPATH
    [Console]::OutputEncoding = [Text.Encoding]::UTF8
    if ($pre.Count) { $env:PYTHONPATH = if ($prevPath) { "$repo;$prevPath" } else { $repo } }
    try {
        $r = & $cmd @pre @args
        $code = $LASTEXITCODE
    } finally {
        [Console]::OutputEncoding = $prevOut
        $env:PYTHONPATH = $prevPath
    }
    if ($code -ne 0 -or -not $r) { return }

    $text = ($r | Out-String).Trim()
    $j = $null
    if ($text.StartsWith('{')) {
        try { $j = $text | ConvertFrom-Json } catch { $j = $null }
    }
    if ($null -eq $j -or $null -eq $j.cd -or $null -eq $j.run) {
        $r  # not a launch line: pass it through
        return
    }
    foreach ($w in @($j.warnings)) { if ($w) { Write-Warning $w } }
    try { Set-Location -LiteralPath $j.cd -ErrorAction Stop } catch { Write-Error $_; return }  # never resume elsewhere
    $run = @($j.run)
    # A launched session must not look like a child of another Claude session (it would not save its transcript):
    # clear the marker and force persistence for the wrapper only, then put this shell's own values back.
    $prevChild = $env:CLAUDE_CODE_CHILD_SESSION
    $prevForce = $env:CLAUDE_CODE_FORCE_SESSION_PERSISTENCE
    $env:CLAUDE_CODE_CHILD_SESSION = $null
    $env:CLAUDE_CODE_FORCE_SESSION_PERSISTENCE = '1'
    try {
        if ($run.Count -gt 1) {
            $rest = $run[1..($run.Count - 1)]  # splat a variable: @(...) inline would pass one array argument
            & $run[0] @rest
        } else {
            & $run[0]
        }
    } finally {
        $env:CLAUDE_CODE_CHILD_SESSION = $prevChild
        $env:CLAUDE_CODE_FORCE_SESSION_PERSISTENCE = $prevForce
    }
}
