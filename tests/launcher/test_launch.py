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


def test_default_wrapper_cc_area_is_clauded_too(fake_env):
    assert default_wrapper(fake_env.root, fake_env.root / "cc") == "clauded"
    assert default_wrapper(fake_env.root, fake_env.root / "CC" / "tool") == "clauded"


def test_default_wrapper_elsewhere_is_clauded(fake_env):
    assert default_wrapper(fake_env.root, fake_env.root / "work" / "net") == "clauded"
    assert default_wrapper(fake_env.root, fake_env.root / "ccx") == "clauded"
    assert default_wrapper(fake_env.root, fake_env.root) == "clauded"


@pytest.mark.parametrize("rel", ["", "cc", "work/net", "work/wf/x", "home/ai"])
def test_default_wrapper_is_clauded_everywhere(fake_env, rel):
    assert default_wrapper(fake_env.root, fake_env.root / rel) == "clauded"


@pytest.mark.parametrize("wrapper, expected", [
    ("clauded", "claude"), ("claude6", "claude"), ("claudew", "claude"),
    ("claude", "claude"), ("claudeb", "claudeb"), ("qwen", "qwen"),
])
def test_without_bypass_maps_each_wrapper(wrapper, expected):
    assert launch.without_bypass(wrapper) == expected


def test_without_bypass_covers_every_known_wrapper():
    assert {launch.without_bypass(w) for w in launch.WRAPPERS} == {"claude", "claudeb", "qwen"}


def test_resume_uses_recorded_wrapper(tmp_path):
    row = make_row(cwd=str(tmp_path), wrapper="claude6")
    launch_ = resume_command(row, None, root=tmp_path)
    assert launch_.argv == ["claude6", "-r", "s1"]
    assert launch_.cwd == tmp_path and launch_.warnings == []


def test_resume_override_beats_recorded_wrapper(tmp_path):
    assert resume_command(make_row(cwd=str(tmp_path), wrapper="claude6"), "claudeb", root=tmp_path).argv == ["claudeb", "-r", "s1"]


def test_resume_falls_back_to_clauded(fake_env):
    in_cc = make_row(cwd=str(fake_env.root / "cc"))
    elsewhere = make_row(cwd=str(fake_env.root / "work" / "net"))
    assert resume_command(in_cc, None, root=fake_env.root).argv[0] == "clauded"
    assert resume_command(elsewhere, None, root=fake_env.root).argv[0] == "clauded"
    assert resume_command(make_row(cwd=str(fake_env.root / "cc")), None, root=fake_env.root).argv == ["clauded", "-r", "s1"]


def test_resume_last_launched_with_plain_claude_resumes_with_clauded(tmp_path):
    assert resume_command(make_row(cwd=str(tmp_path), wrapper="claude"), None, root=tmp_path).argv == ["clauded", "-r", "s1"]


@pytest.mark.parametrize("last", ["claudeb", "claude6", "claudew"])
def test_resume_reuses_a_last_account_wrapper(tmp_path, last):
    assert resume_command(make_row(cwd=str(tmp_path), wrapper=last), None, root=tmp_path).argv == [last, "-r", "s1"]


def test_resume_explicit_wrapper_beats_plain_claude_last_wrapper(tmp_path):
    row = make_row(cwd=str(tmp_path), wrapper="claude")
    assert resume_command(row, "claudeb", root=tmp_path).argv == ["claudeb", "-r", "s1"]
    assert resume_command(row, "claude", root=tmp_path).argv == ["claude", "-r", "s1"]


def test_resume_qwen_ignores_a_last_wrapper(tmp_path):
    row = make_row(store="qwen", sid="q1", cwd=str(tmp_path), wrapper="claudeb")
    assert resume_command(row, None, root=tmp_path).argv == ["qwen", "-r", "q1"]


def test_resume_qwen(tmp_path):
    row = make_row(store="qwen", sid="q1", cwd=str(tmp_path))
    assert resume_command(row, None, root=tmp_path).argv == ["qwen", "-r", "q1"]
    assert resume_command(row, "claude", root=tmp_path).argv == ["qwen", "-r", "q1"]  # a Qwen session is never resumed by Claude


def test_new_command_keeps_name_verbatim(tmp_path):
    name = 'fix "auth" $env thing'
    assert new_command(tmp_path, name, "clauded").argv == ["clauded", "-n", name]


def test_new_command_with_session_id(tmp_path):
    launch_ = new_command(tmp_path, "n", "claudeb", session_id="abc-123")
    assert launch_.argv == ["claudeb", "-n", "n", "--session-id", "abc-123"]
    assert launch_.cwd == tmp_path


def test_new_command_qwen_has_no_name_flag(tmp_path):
    launch_ = new_command(tmp_path, "n", "qwen", session_id="abc-123")
    assert launch_.argv == ["qwen", "--session-id", "abc-123"]  # Qwen Code has no -n
    assert new_command(tmp_path, "n", "qwen").argv == ["qwen"]
    assert launch_.cwd == tmp_path


def test_missing_cwd_adds_warning_and_still_returns_command(tmp_path):
    gone = tmp_path / "gone"
    launch_ = resume_command(make_row(cwd=str(gone), cwd_exists=False), "clauded", root=tmp_path)
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


def test_default_wrapper_outside_root_is_clauded(fake_env, tmp_path):
    assert default_wrapper(fake_env.root, tmp_path / "elsewhere" / "cc") == "clauded"


def test_resume_without_recorded_folder_warns_and_uses_dot(tmp_path):
    launch_ = resume_command(make_row(cwd=None), None, root=tmp_path)
    assert launch_.cwd == Path(".")
    assert launch_.warnings == ["session has no recorded folder"]
    assert launch_.argv == ["clauded", "-r", "s1"]
