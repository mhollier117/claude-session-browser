import json
from pathlib import Path

import pytest

import termfocus
from launcher import launch
from launcher.index import Row
from launcher.launch import (Launch, default_wrapper, focus_live, new_command, resume_command,
                             to_shell_result)


def make_row(**kw) -> Row:
    base = dict(store="claude", sid="s1", cwd=r"C:\proj", name=None, display_name="x", first_ask="", last_ask="",
                last_reply="", user_msgs=1, assistant_msgs=1, size=1, mtime=1.0, last_activity=1.0,
                live_pid=None, wrapper=None, cwd_exists=True)
    base.update(kw)
    return Row(**base)


def test_wrappers_constant():
    assert launch.WRAPPERS == ("claude", "clauded", "claude6", "claudeb", "claudew", "qwen")


def test_default_wrapper_cc_area_is_claude(fake_env):
    assert default_wrapper(fake_env.root, fake_env.root / "cc") == "claude"
    assert default_wrapper(fake_env.root, fake_env.root / "CC" / "tool") == "claude"


def test_default_wrapper_elsewhere_is_clauded(fake_env):
    assert default_wrapper(fake_env.root, fake_env.root / "work" / "net") == "clauded"
    assert default_wrapper(fake_env.root, fake_env.root / "ccx") == "clauded"
    assert default_wrapper(fake_env.root, fake_env.root) == "clauded"


def test_resume_uses_recorded_wrapper(tmp_path):
    row = make_row(cwd=str(tmp_path), wrapper="claude6")
    launch_ = resume_command(row, None)
    assert launch_.argv == ["claude6", "-r", "s1"]
    assert launch_.cwd == tmp_path and launch_.warnings == []


def test_resume_override_beats_recorded_wrapper(tmp_path):
    assert resume_command(make_row(cwd=str(tmp_path), wrapper="claude6"), "claudeb").argv == ["claudeb", "-r", "s1"]


def test_resume_falls_back_to_default_wrapper(fake_env):
    in_cc = make_row(cwd=str(fake_env.root / "cc"))
    elsewhere = make_row(cwd=str(fake_env.root / "work" / "net"))
    assert resume_command(in_cc, None, root=fake_env.root).argv[0] == "claude"
    assert resume_command(elsewhere, None, root=fake_env.root).argv[0] == "clauded"
    assert resume_command(elsewhere, None).argv[0] == "clauded"  # no root: the general default


def test_resume_qwen(tmp_path):
    row = make_row(store="qwen", sid="q1", cwd=str(tmp_path))
    assert resume_command(row, None).argv == ["qwen", "-r", "q1"]
    assert resume_command(row, "claude").argv == ["qwen", "-r", "q1"]  # a Qwen session is never resumed by Claude


def test_new_command_keeps_name_verbatim(tmp_path):
    name = 'fix "auth" $env thing'
    assert new_command(tmp_path, name, "clauded").argv == ["clauded", "-n", name]


def test_new_command_with_session_id(tmp_path):
    launch_ = new_command(tmp_path, "n", "qwen", session_id="abc-123")
    assert launch_.argv == ["qwen", "-n", "n", "--session-id", "abc-123"]
    assert launch_.cwd == tmp_path


def test_missing_cwd_adds_warning_and_still_returns_command(tmp_path):
    gone = tmp_path / "gone"
    launch_ = resume_command(make_row(cwd=str(gone), cwd_exists=False), "clauded")
    assert launch_.warnings == [f"folder no longer exists: {gone}"]
    assert launch_.argv == ["clauded", "-r", "s1"]


def test_to_shell_result_is_json_with_no_quoting(tmp_path):
    name = 'a "b" $c `d` é'
    out = to_shell_result(Launch(tmp_path, ["clauded", "-n", name], ["w"]))
    assert json.loads(out) == {"cd": str(tmp_path), "run": ["clauded", "-n", name], "warnings": ["w"]}
    assert "é" in out  # ensure_ascii=False


def test_focus_live_delegates(monkeypatch):
    seen = []
    monkeypatch.setattr(termfocus, "focus_session", lambda pid, cwd=None: seen.append((pid, cwd)) or {"ok": True})
    assert focus_live(make_row(live_pid=4242, cwd=r"C:\proj")) == {"ok": True}
    assert seen == [(4242, r"C:\proj")]


def test_focus_live_without_pid_does_not_call_termfocus(monkeypatch):
    monkeypatch.setattr(termfocus, "focus_session", lambda *a, **k: pytest.fail("must not be called"))
    result = focus_live(make_row(live_pid=None))
    assert result["ok"] is False and result["error"]
