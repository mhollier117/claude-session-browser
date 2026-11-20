"""tools/cc.ps1: the PowerShell `cc` function. Nothing here runs it; the file is read and its hand-off pinned."""
import re
from pathlib import Path

CC_PS1 = Path(__file__).resolve().parents[2] / "tools" / "cc.ps1"


def _text() -> str:
    return CC_PS1.read_text(encoding="utf-8")


def _body() -> str:
    """The cc function's body, without comment lines."""
    return "\n".join(line for line in _text().splitlines() if not line.lstrip().startswith("#"))


def test_windows_powershell_hands_over_to_pwsh_first():
    body = _body()
    check = body.index("if ($PSVersionTable.PSVersion.Major -lt 7) {")
    assert check < body.index("$repo = Split-Path -Parent $PSScriptRoot")  # before anything else runs
    assert "Get-Command pwsh -CommandType Application" in body
    handoff = body[check:body.index("$repo = ")]
    # the arguments go as single-quoted literals in a base64 -EncodedCommand: no double quote on the command line
    assert "pwsh -NoLogo -EncodedCommand ([Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($text)))" \
        in handoff
    assert "$text = (@('cc') + $quoted) -join ' '" in handoff
    assert """"'" + ([string]$_ -replace $sq, '$0$0') + "'\"""" in handoff  # every quote doubled
    assert "return" in handoff.split("EncodedCommand", 1)[1].split("}", 1)[0]  # pwsh's exit code is cc's
    assert "-NoProfile" not in handoff  # the child loads the PowerShell 7 profile, which defines cc


def test_handoff_is_not_a_scriptblock():
    """`pwsh -Command { ... } -args` from 5.1 runs the child with `-inputFormat xml`: its stdin is a pipe and cc's
    screens (which need the console) would refuse to start."""
    assert not re.search(r"pwsh[^\n]*-Command\s*\{", _body())


def test_without_pwsh_one_line_and_continue_in_5_1():
    body = _body()
    handoff = body[body.index("if ($PSVersionTable.PSVersion.Major -lt 7) {"):body.index("$repo = ")]
    [line] = [x for x in handoff.splitlines() if "Write-Host" in x]
    assert "cc needs PowerShell 7" in line and "double quotes" in line
    assert handoff.rstrip().endswith("}")  # no return after the message: cc goes on in Windows PowerShell


def test_file_is_ascii_for_windows_powershell():
    """5.1 reads a script without a BOM in the ANSI code page: keep every byte ASCII (the typographic quotes are
    written as [char] codes)."""
    assert all(b < 128 for b in CC_PS1.read_bytes())
    assert "[char]0x2018 + [char]0x2019 + [char]0x201A + [char]0x201B" in _text()
