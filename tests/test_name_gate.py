"""Tests for hooks/csb-name-gate.ps1 (UserPromptSubmit naming gate).

Runs the real script under Windows PowerShell 5.1 against a fake
CLAUDE_CONFIG_DIR with a fake session registry. Hook input shapes are the
ones captured live from Claude Code 2.1.316 (2026-11-01).
"""
import json
import os
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE = os.path.join(ROOT, "hooks", "csb-name-gate.ps1")
PS = shutil.which("powershell.exe")

pytestmark = pytest.mark.skipif(sys.platform != "win32" or not PS,
                                reason="needs Windows PowerShell")

SID = "59d18fd6-5029-47d1-91ec-9acc4fbf0673"


def _input(**over):
    d = {"session_id": SID, "transcript_path": "x.jsonl", "cwd": "C:\\x",
         "prompt_id": "p", "permission_mode": "default",
         "hook_event_name": "UserPromptSubmit", "prompt": "do the thing"}
    d.update(over)
    return {k: v for k, v in d.items() if v is not None}


def _registry(cfg, entrypoint="cli", sid=SID, kind="interactive", pid=1960):
    os.makedirs(os.path.join(cfg, "sessions"), exist_ok=True)
    with open(os.path.join(cfg, "sessions", "%d.json" % pid), "w") as fh:
        json.dump({"pid": pid, "sessionId": sid, "kind": kind,
                   "entrypoint": entrypoint, "status": "idle"}, fh)


def _run(cfg, stdin, env_extra=None, home=None):
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_PID", "CSB_NAME_GATE")}
    env["CLAUDE_CONFIG_DIR"] = cfg
    env["USERPROFILE"] = home or cfg
    env.update(env_extra or {})
    data = stdin if isinstance(stdin, (bytes, str)) else json.dumps(stdin)
    if isinstance(data, str):
        data = data.encode("utf-8")
    p = subprocess.run([PS, "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-File", GATE], input=data, capture_output=True,
                       env=env, timeout=30)
    return p.returncode, p.stderr.decode("utf-8", "replace")


@pytest.fixture
def cfg(tmp_path):
    return str(tmp_path)


def test_blocks_unnamed_cli_prompt(cfg):
    _registry(cfg)
    code, err = _run(cfg, _input())
    assert code == 2
    assert "/rename" in err


def test_allows_named_session(cfg):
    _registry(cfg)
    assert _run(cfg, _input(session_title="gate-named"))[0] == 0


def test_blank_title_counts_as_unnamed(cfg):
    _registry(cfg)
    assert _run(cfg, _input(session_title="   "))[0] == 2


@pytest.mark.parametrize("prompt", ["/rename foo", "  /help", "!dir"])
def test_allows_slash_and_shell_commands(cfg, prompt):
    _registry(cfg)
    assert _run(cfg, _input(prompt=prompt))[0] == 0


def test_allows_print_mode_sdk_entrypoint(cfg):
    _registry(cfg, entrypoint="sdk-cli")
    assert _run(cfg, _input())[0] == 0


def test_allows_non_interactive_kind(cfg):
    _registry(cfg, kind="background")
    assert _run(cfg, _input())[0] == 0


def test_allows_when_no_registry_entry(cfg):
    _registry(cfg, sid="someone-else")
    assert _run(cfg, _input())[0] == 0


def test_allows_when_no_registry_dir(cfg):
    assert _run(cfg, _input())[0] == 0


def test_claude_pid_fast_path(cfg):
    _registry(cfg, pid=4242)
    assert _run(cfg, _input(), {"CLAUDE_PID": "4242"})[0] == 2


def test_stale_claude_pid_falls_back_to_scan(cfg):
    _registry(cfg, sid="other", pid=4242)
    _registry(cfg, pid=5555)
    assert _run(cfg, _input(), {"CLAUDE_PID": "4242"})[0] == 2


def test_non_user_source_passes(cfg):
    _registry(cfg)
    assert _run(cfg, _input(source="queued"))[0] == 0


@pytest.mark.parametrize("prompt", [
    '<cross-session-message from="uds:\\\\.\\pipe\\LOCAL\\cc-msg-1" from-name="peer">\nport the fix',
    '  <cross-session-message from="x">hi</cross-session-message>',
])
def test_cross_session_message_passes(cfg, prompt):
    # Another Claude session's message, not something the user typed: blocking
    # it just bounces the message (seen live 2026-11-03, Claude Code 2.1.311).
    _registry(cfg)
    assert _run(cfg, _input(prompt=prompt))[0] == 0


def test_other_event_passes(cfg):
    _registry(cfg)
    assert _run(cfg, _input(hook_event_name="Stop"))[0] == 0


@pytest.mark.parametrize("raw", ["", "not json", "{", "[1,2]", b"\xff\xfe\x00"])
def test_fails_open_on_bad_input(cfg, raw):
    _registry(cfg)
    assert _run(cfg, raw)[0] == 0


def test_corrupt_registry_file_is_skipped(cfg):
    _registry(cfg, pid=5555)
    with open(os.path.join(cfg, "sessions", "1.json"), "w") as fh:
        fh.write("{broken")
    assert _run(cfg, _input())[0] == 2


def test_env_off_switch(cfg):
    _registry(cfg)
    assert _run(cfg, _input(), {"CSB_NAME_GATE": "0"})[0] == 0


def test_file_off_switch_in_config_dir(cfg):
    _registry(cfg)
    open(os.path.join(cfg, "name-gate.off"), "w").close()
    assert _run(cfg, _input())[0] == 0


def test_file_off_switch_in_home(cfg, tmp_path_factory):
    _registry(cfg)
    home = str(tmp_path_factory.mktemp("home"))
    os.makedirs(os.path.join(home, ".claude"))
    open(os.path.join(home, ".claude", "name-gate.off"), "w").close()
    assert _run(cfg, _input(), home=home)[0] == 0


def test_unicode_prompt(cfg):
    _registry(cfg)
    assert _run(cfg, _input(prompt="Grüße – ✓ 日本"))[0] == 2
    assert _run(cfg, _input(prompt="Grüße", session_title="Prüfung ✓"))[0] == 0
