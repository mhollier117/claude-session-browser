"""Sessions labelled and resumed by the alias that launched them, from the PowerShell profile's marker in the
claude.exe environment (CLAUDE_RESTORE_LAUNCHER / CLAUDE_RESTORE_ARGS): any claude* alias, no table to extend."""
import json
import sqlite3

import pytest

import launcher.index as index_mod
import launcher.launch as launch
from launcher.index import Index, Row
from launcher.launch import resume_command
from launcher_helpers import write_claude_session


def _u(text, cwd):
    return {"type": "user", "message": {"role": "user", "content": text}, "cwd": str(cwd)}


def _a(text):
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def _live_one(env, sid, pid, cwd):
    rec = {"pid": pid, "sessionId": sid, "cwd": str(cwd), "kind": "interactive"}
    (env.claude_home / "sessions" / f"{pid}.json").write_text(json.dumps(rec), encoding="utf-8")
    return [{"session_id": sid, "pid": pid, "name": "x", "named": False, "cwd": str(cwd)}]


def _idx(env, live, launch_env):
    return Index(env, claude_live=lambda: live, pid_exists=lambda p: False, launch_env=launch_env)


def make_row(**kw) -> Row:
    base = dict(store="claude", sid="s1", cwd=r"C:\proj", name=None, display_name="x", first_ask="", last_ask="",
                last_reply="", user_msgs=1, assistant_msgs=1, size=1, mtime=1.0, last_activity=1.0,
                live_pid=None, wrapper=None, cwd_exists=True)
    base.update(kw)
    return Row(**base)


# ---- recording ---------------------------------------------------------------------------------------------------

def test_live_launch_recorded_with_alias_and_args(fake_env):
    cwd = fake_env.root / "home" / "ai"
    write_claude_session(fake_env, cwd, "k1", [_u("hi", cwd), _a("hello")])
    live = _live_one(fake_env, "k1", 41, cwd)
    seen = []

    def env_of(pid):
        seen.append(pid)
        return {"launcher": "claudek", "args": '["--model","k3 pro","--nb"]'}

    idx = _idx(fake_env, live, env_of)
    idx.refresh()
    row = idx.row("claude", "k1")
    assert (row.wrapper, row.launch_args) == ("claudek", ["--model", "k3 pro", "--nb"])
    assert index_mod.type_label(row) == "claudek"
    idx.refresh()
    assert seen == [41]  # read once per live process
    assert idx.recorded_wrappers() == ["claudek"]


def test_live_launch_without_marker_or_with_a_bad_name_is_left_alone(fake_env):
    cwd = fake_env.root / "home" / "ai"
    for sid in ("a1", "b1"):
        write_claude_session(fake_env, cwd, sid, [_u("hi", cwd), _a("hello")])
    live = _live_one(fake_env, "a1", 51, cwd) + _live_one(fake_env, "b1", 52, cwd)
    envs = {51: {"launcher": None, "args": None}, 52: {"launcher": "rm -rf; claude", "args": "[]"}}
    idx = _idx(fake_env, live, envs.get)
    idx.refresh()
    assert sorted((r.sid, r.wrapper, r.launch_args) for r in idx.rows()) == [("a1", None, None), ("b1", None, None)]


def test_live_launch_unreadable_process_is_retried(fake_env):
    cwd = fake_env.root / "home" / "ai"
    write_claude_session(fake_env, cwd, "z1", [_u("hi", cwd), _a("hello")])
    live = _live_one(fake_env, "z1", 61, cwd)
    answers = [None, {"launcher": "claudez", "args": None}]
    idx = _idx(fake_env, live, lambda pid: answers.pop(0))
    idx.refresh()
    assert idx.row("claude", "z1").wrapper is None
    idx.refresh()
    row = idx.row("claude", "z1")
    assert (row.wrapper, row.launch_args) == ("claudez", None)  # launched before the profile recorded args


def test_record_launch_clears_the_previous_launch_args(fake_env):
    cwd = fake_env.root / "home" / "ai"
    write_claude_session(fake_env, cwd, "r1", [_u("hi", cwd), _a("hello")])
    live = _live_one(fake_env, "r1", 71, cwd)
    idx = _idx(fake_env, live, lambda pid: {"launcher": "claudek", "args": '["--nb"]'})
    idx.refresh()
    idx.record_launch("claude", "r1", "claude6")
    row = idx.row("claude", "r1")
    assert (row.wrapper, row.launch_args) == ("claude6", None)


def test_launch_args_column_added_to_an_older_file(fake_env):
    path = index_mod.index_file(fake_env.data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute(f"PRAGMA user_version = {index_mod._SCHEMA_VERSION}")
    db.execute(index_mod._SCHEMA.replace(" launch_args TEXT,", ""))
    db.execute("INSERT INTO sessions (store, sid, path, wrapper) VALUES ('claude', 'old', 'p', 'claude6')")
    db.commit()
    db.close()
    idx = Index(fake_env, claude_live=lambda: [], pid_exists=lambda p: False)
    assert "launch_args" in {r[1] for r in idx._db.execute("PRAGMA table_info(sessions)")}
    assert idx._db.execute("SELECT wrapper FROM sessions WHERE sid = 'old'").fetchone()[0] == "claude6"  # kept


def test_alias_label_learned_and_applied_to_older_sessions_of_that_alias(fake_env):
    cwd = fake_env.root / "home" / "ai"
    for sid in ("old", "new", "zz"):
        write_claude_session(fake_env, cwd, sid, [_u("hi", cwd), _a("hello")])
    live = _live_one(fake_env, "old", 81, cwd)
    envs = {81: {"launcher": "claudek", "args": None, "label": None}}  # launched before labels existed
    idx = _idx(fake_env, live, lambda pid: envs.get(pid))
    idx.refresh()
    assert index_mod.type_label(idx.row("claude", "old")) == "claudek"  # no label known yet: the alias name
    live[:] = _live_one(fake_env, "new", 82, cwd) + _live_one(fake_env, "zz", 83, cwd)
    envs[82] = {"launcher": "claudek", "args": "[]", "label": "kimi"}
    envs[83] = {"launcher": "claudez", "args": "[]", "label": "bad label!"}  # not a label: ignored
    idx.refresh()
    labels = {r.sid: index_mod.type_label(r) for r in idx.rows()}
    assert labels == {"old": "kimi", "new": "kimi", "zz": "claudez"}


def test_type_label():
    lab = lambda **kw: index_mod.type_label(make_row(**kw))
    assert lab(wrapper="claudek") == "claudek"
    assert lab(wrapper="claudenewthing") == "claudenewthing"
    assert lab(wrapper="clauded") == "claude" and lab(wrapper="claude") == "claude"
    assert lab(wrapper=None, backend="glm") == "glm"
    assert lab(store="qwen", wrapper="qwen") == "qwencli"
    assert lab(wrapper="claudek", label="kimi") == "kimi"
    assert lab(wrapper="claudez", label="glm") == "glm"  # the same type as an old session guessed from its model
    assert lab(wrapper="claudek", label="no good!") == "claudek"


# ---- resume ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("typed, kept", [
    ([], []),
    (["--nb"], ["--nb"]),
    (["fix the bug"], []),  # the initial prompt is not repeated
    (["--model", "k3 pro", "fix it"], ["--model", "k3 pro"]),
    (["-c"], []),
    (["-r", "abc", "--nb"], ["--nb"]),
    (["--resume=abc", "--verbose", "go"], ["--verbose"]),
    (["--session-id", "u-1", "-n", "Name", "--fork-session"], []),
    (["--mcp-config", "a.json", "b.json", "--nb"], ["--mcp-config", "a.json", "b.json", "--nb"]),
    (["--add-dir", "C:\\x", "C:\\y"], ["--add-dir", "C:\\x", "C:\\y"]),
    (["--brandnew", "value", "--nb"], ["--brandnew", "value", "--nb"]),  # an unknown option keeps its value
    (["--settings=s.json", "-p", "say hi"], ["--settings=s.json"]),
    (["-w", "feature", "--effort", "high"], ["--effort", "high"]),
])
def test_relaunch_args(typed, kept):
    assert launch.relaunch_args(typed) == kept


def test_resume_repeats_the_recorded_alias_and_args(tmp_path):
    row = make_row(cwd=str(tmp_path), wrapper="claudek", launch_args=["--model", "k3", "first prompt"])
    argv = resume_command(row, None, root=tmp_path).argv
    assert argv == ["claudek", "--model", "k3", "-r", "s1"]
    assert "--dangerously-skip-permissions" not in argv  # the profile's `claude` adds it unless --nb was typed
    assert resume_command(row, None, root=tmp_path, ask_permissions=True).argv == \
        ["claudek", "--model", "k3", "--nb", "-r", "s1"]


def test_resume_recorded_plain_claude_and_a_new_alias(tmp_path):
    plain = make_row(cwd=str(tmp_path), wrapper="claude", launch_args=["--nb"])
    assert resume_command(plain, None, root=tmp_path).argv == ["claude", "--nb", "-r", "s1"]
    new = make_row(cwd=str(tmp_path), wrapper="claudenew", launch_args=[])
    assert resume_command(new, None, root=tmp_path).argv == ["claudenew", "-r", "s1"]


def test_resume_explicit_wrapper_and_unrecorded_rows_keep_the_old_rules(tmp_path):
    row = make_row(cwd=str(tmp_path), wrapper="claudek", launch_args=["--nb"])
    assert resume_command(row, "claude6", root=tmp_path).argv == ["claude6", "-r", "s1"]  # ctrl-w still wins
    legacy = make_row(cwd=str(tmp_path), wrapper="claudez", backend="glm")  # no launch_args: as before
    assert resume_command(legacy, None, root=tmp_path).argv == ["claudez", "--dangerously-skip-permissions", "-r", "s1"]
    # only a claude* name takes the recorded path (the index stores only such names); anything else is the old rule
    odd = make_row(cwd=str(tmp_path), wrapper="not-claude", launch_args=["--nb"])
    assert resume_command(odd, None, root=tmp_path).argv == ["not-claude", "-r", "s1"]
