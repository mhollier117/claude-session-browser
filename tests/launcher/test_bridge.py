"""launcher.bridge: the pure glue behind the browser's launcher API (no webview needed)."""
import json
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from launcher import bridge
from launcher.index import Index, LAUNCHED_FILE
from launcher.scaffold import Result
from launcher.suggest import Suggestion
from launcher_helpers import make_row, write_qwen_session


class FakeRunner:
    """Records commands; every one succeeds with no output. Never runs anything."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def run(self, args, cwd, env=None, timeout=60, input=None):
        self.calls.append(list(args))
        return Result(0, "", "")


def _q_user(text):
    return {"type": "user", "provenance": "real_user", "message": {"role": "user", "parts": [{"text": text}]}}


def _index(env):
    return Index(env, claude_live=lambda: [], pid_exists=lambda pid: False)


# ---- PowerShell quoting -----------------------------------------------------------------------------------------

def test_ps_command_single_quotes_every_argument():
    assert bridge.ps_command(["clauded", "-n", "my session"]) == "& 'clauded' '-n' 'my session'"


@pytest.mark.parametrize("name, quoted", [
    ("it's", "'it''s'"),
    ('say "hi"', "'say \"hi\"'"),
    ("$env:USERNAME", "'$env:USERNAME'"),
    ("back`tick", "'back`tick'"),
    ("a ; b & c | d", "'a ; b & c | d'"),
    ("curly \u2018x\u2019 \u201ay\u201b", "'curly \u2018\u2018x\u2019\u2019 \u201a\u201ay\u201b\u201b'"),
])
def test_ps_command_quotes_special_characters(name, quoted):
    assert bridge.ps_command(["clauded", "-n", name]) == "& 'clauded' '-n' " + quoted


def test_ps_command_needs_a_command():
    with pytest.raises(ValueError):
        bridge.ps_command([])


@pytest.mark.skipif(not shutil.which("pwsh"), reason="pwsh not installed")
def test_ps_command_round_trips_through_pwsh():
    """The quoted command, run by real pwsh from an argv list, hands every argument back unchanged."""
    args = ["-n", "it's", 'say "hi"', "$env:USERNAME", "back`tick", "a ; b & c", "\u2019quoted\u2019", "two  spaces"]
    script = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
              "function show { foreach ($a in $args) { [Console]::Out.WriteLine('<' + $a + '>') } }; "
              + bridge.ps_command(["show", *args]))
    out = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", script],
                         capture_output=True, timeout=60).stdout.decode("utf-8")
    assert out.splitlines() == [f"<{a}>" for a in args]


# ---- terminal argv ----------------------------------------------------------------------------------------------

def test_wt_argv_opens_a_tab_running_powershell(tmp_path):
    argv = bridge.wt_argv(tmp_path, ["clauded", "-n", "x"], "pwsh")
    assert argv == ["wt", "-w", "0", "nt", "-d", str(tmp_path), "pwsh", "-NoExit", "-Command",
                    "& 'clauded' '-n' 'x'"]


def test_wt_argv_escapes_semicolons_for_wt():
    """wt splits its command line at every unescaped `;`, even inside one argument."""
    argv = bridge.wt_argv(Path(r"C:\a;b"), ["clauded", "-n", "one; two"], "pwsh")
    assert argv[5] == r"C:\a\;b"
    assert argv[-1] == r"& 'clauded' '-n' 'one\; two'"


def test_console_argv_has_no_wt():
    assert bridge.console_argv(["qwen", "-r", "s1"], "powershell") == [
        "powershell", "-NoExit", "-Command", "& 'qwen' '-r' 's1'"]


def test_pick_shell_prefers_pwsh():
    assert bridge.pick_shell(which=lambda name: "C:/pwsh.exe" if name == "pwsh" else None) == "pwsh"
    assert bridge.pick_shell(which=lambda name: None) == "powershell"


# ---- paths -------------------------------------------------------------------------------------------------------

def test_resolve_rel(fake_env):
    root = fake_env.root
    assert bridge.resolve_rel(root, "") == root
    assert bridge.resolve_rel(root, None) == root
    assert bridge.resolve_rel(root, "work/net") == root / "work" / "net"
    assert bridge.resolve_rel(root, "WORK\\Net") == root / "work" / "net"
    assert bridge.resolve_rel(root, "/work/net/") == root / "work" / "net"


@pytest.mark.parametrize("rel", ["work/nope", "../root", "work/../..", 5, r"C:\Windows"])
def test_resolve_rel_rejects(fake_env, rel):
    with pytest.raises(ValueError):
        bridge.resolve_rel(fake_env.root, rel)


def test_rel_of(fake_env):
    root = fake_env.root
    assert bridge.rel_of(root, root) == ""
    assert bridge.rel_of(root, root / "work" / "net") == "work/net"


# ---- serialisation -----------------------------------------------------------------------------------------------

def test_suggestion_dict_is_relative_to_root(fake_env):
    s = Suggestion(fake_env.root / "work" / "net", "vpn_fix", "high", "because", "qwen")
    assert bridge.suggestion_dict(fake_env.root, s) == {
        "path": "work/net", "new_folder": "vpn_fix", "confidence": "high", "reason": "because", "source": "qwen"}
    root = Suggestion(fake_env.root, None, "low", "start at the root", "root")
    assert bridge.suggestion_dict(fake_env.root, root)["path"] == ""


def test_row_dict_adds_tool():
    d = bridge.row_dict(make_row("C:/x", "hello", 5.0, sid="s1"))
    assert d["tool"] == "claude" and d["sid"] == "s1" and d["display_name"] == "hello"
    json.dumps(d)  # goes over the js bridge


def test_qwen_session_dicts_take_the_browser_shape(fake_env):
    ai = fake_env.root / "home" / "ai"
    write_qwen_session(fake_env, ai, "q1", [_q_user("hello qwen")])
    idx = _index(fake_env)
    idx.refresh()
    rows = idx.rows()
    rows.append(make_row("C:/elsewhere", "a claude row", 1.0, sid="c1"))
    [d] = bridge.qwen_session_dicts(rows, when=lambda m: f"at {m:.0f}")
    q = rows[0]
    assert d == {"id": "q1", "auto_title": "hello qwen", "display_title": "hello qwen", "first_user": "hello qwen",
                 "cwd": str(ai), "mtime": q.mtime, "when": f"at {q.mtime:.0f}", "user_msgs": 1, "assistant_msgs": 0,
                 "total_msgs": 1, "project": "ai", "color": "", "tool": "qwen"}
    idx.close()


def test_children_listing(fake_env):
    idx = _index(fake_env)
    listing = bridge.children_listing(fake_env, idx, fake_env.root / "work")
    assert listing["ok"] is True and listing["rel"] == "work"
    assert [c["name"] for c in listing["children"]] == ["clients", "halo", "net", "vendors"]
    net = listing["children"][2]
    assert net == {"name": "net", "rel": "work/net", "kind": "bucket", "sessions": 0, "last_activity": None,
                   "names": []}
    assert listing["start_here"] is True
    assert listing["kind_options"] == ["organisational"]
    assert listing["default_wrapper"] == "clauded"
    top = bridge.children_listing(fake_env, idx, fake_env.root)
    assert top["start_here"] is False and top["rel"] == ""
    assert bridge.children_listing(fake_env, idx, fake_env.root / "cc")["default_wrapper"] == "claude"
    idx.close()


# ---- new-session payload ------------------------------------------------------------------------------------------

def _payload(**over):
    p = {"name": "fix vpn", "rel": "work/net", "new_folder": None, "kind": None, "wrapper": "clauded", "github": None}
    p.update(over)
    return p


def test_parse_new_start_here(fake_env):
    req = bridge.parse_new(fake_env.root, _payload(name="  fix vpn  "))
    assert (req.name, req.folder, req.new_folder, req.launches) == ("fix vpn", fake_env.root / "work" / "net",
                                                                    None, True)


def test_parse_new_new_project(fake_env):
    req = bridge.parse_new(fake_env.root, _payload(rel="home/ai", new_folder="llm", kind="project",
                                                   github="personal"))
    assert (req.new_folder, req.kind, req.github, req.launches) == ("llm", "project", "personal", True)


def test_parse_new_container_does_not_launch_and_needs_no_wrapper(fake_env):
    req = bridge.parse_new(fake_env.root, _payload(rel="home", new_folder="box", kind="container", wrapper=None))
    assert req.launches is False


@pytest.mark.parametrize("over, problem", [
    ({"name": ""}, "name"),
    ({"name": "   "}, "name"),
    ({"name": 7}, "name"),
    ({"name": "two\nlines"}, "control"),
    ({"rel": "work/nope"}, "folder"),
    ({"rel": ""}, "root"),
    ({"wrapper": "bash"}, "wrapper"),
    ({"wrapper": None}, "wrapper"),
    ({"github": "other"}, "github"),
    ({"new_folder": "x", "kind": "project"}, "kind"),  # work/net only allows organisational
    ({"rel": "home", "new_folder": "x", "kind": None}, "kind"),
    ({"rel": "home", "new_folder": "a/b", "kind": "project"}, "folder name"),
    ({"rel": "home", "new_folder": "ai", "kind": "project"}, "exists"),
])
def test_parse_new_rejects(fake_env, over, problem):
    with pytest.raises(ValueError, match=problem):
        bridge.parse_new(fake_env.root, _payload(**over))


def test_parse_new_rejects_a_non_object(fake_env):
    with pytest.raises(ValueError):
        bridge.parse_new(fake_env.root, ["x"])


def test_prepare_new_start_here_remembers_the_wrapper(fake_env):
    sid = str(uuid.UUID(int=1))
    result, launch = bridge.prepare_new(fake_env, _payload(), runner=FakeRunner(), session_id=sid)
    net = fake_env.root / "work" / "net"
    assert result == {"ok": True, "path": str(net), "rel": "work/net", "created": False, "warnings": [],
                      "launched": True}
    assert launch.cwd == net and launch.argv == ["clauded", "-n", "fix vpn", "--session-id", sid]
    assert json.loads((fake_env.data_dir / LAUNCHED_FILE).read_text(encoding="utf-8")) == {f"claude:{sid}": "clauded"}


def test_prepare_new_qwen_is_remembered_under_qwen(fake_env):
    _result, launch = bridge.prepare_new(fake_env, _payload(wrapper="qwen"), runner=FakeRunner(), session_id="q9")
    assert launch.argv == ["qwen", "--session-id", "q9"]
    assert json.loads((fake_env.data_dir / LAUNCHED_FILE).read_text(encoding="utf-8")) == {"qwen:q9": "qwen"}


def test_prepare_new_creates_a_project_and_launches_in_it(fake_env):
    runner = FakeRunner()
    result, launch = bridge.prepare_new(
        fake_env, _payload(rel="home/ai", new_folder="llm", kind="project", github=None), runner=runner)
    llm = fake_env.root / "home" / "ai" / "llm"
    assert (llm / "plan.md").is_file()
    assert result["created"] is True and result["launched"] is True and result["rel"] == "home/ai/llm"
    assert launch.cwd == llm and launch.argv[:3] == ["clauded", "-n", "fix vpn"]
    assert ["git", "init", "-b", "main"] in runner.calls
    assert not any(c[0] == "gh" for c in runner.calls)


def test_prepare_new_container_creates_and_does_not_launch(fake_env):
    result, launch = bridge.prepare_new(
        fake_env, _payload(rel="home", new_folder="box", kind="container", wrapper=None), runner=FakeRunner())
    assert launch is None
    assert result == {"ok": True, "path": str(fake_env.root / "home" / "box"), "rel": "home/box", "created": True,
                      "warnings": [], "launched": False}
    assert (fake_env.root / "home" / "box").is_dir()
    assert not (fake_env.data_dir / LAUNCHED_FILE).exists()


def test_prepare_new_holds_the_index_lock_only_for_launched_json(fake_env, monkeypatch):
    events = []

    class Lock:
        def __enter__(self):
            events.append("lock")

        def __exit__(self, *exc):
            events.append("unlock")
    real_create, real_remember = bridge.create_folder, bridge.remember_launch
    monkeypatch.setattr(bridge, "create_folder", lambda *a, **k: events.append("create") or real_create(*a, **k))
    monkeypatch.setattr(bridge, "remember_launch", lambda *a: events.append("remember") or real_remember(*a))
    bridge.prepare_new(fake_env, _payload(rel="home/ai", new_folder="llm", kind="project"), runner=FakeRunner(),
                       index_lock=Lock())
    assert events == ["create", "lock", "remember", "unlock"]


def test_prepare_new_errors_become_results(fake_env):
    result, launch = bridge.prepare_new(fake_env, _payload(name=""), runner=FakeRunner())
    assert launch is None and result["ok"] is False and "name" in result["error"]


def test_prepare_new_mkdir_failure_is_an_error(fake_env, monkeypatch):
    def boom(*a, **k):
        raise PermissionError("denied")
    monkeypatch.setattr(bridge, "create_folder", boom)
    result, launch = bridge.prepare_new(
        fake_env, _payload(rel="home", new_folder="box", kind="container"), runner=FakeRunner())
    assert launch is None and result["ok"] is False and "denied" in result["error"]


# ---- pending suggestions ------------------------------------------------------------------------------------------

class FakePending:
    def __init__(self, answer=None):
        self.answer = answer

    def wait(self, timeout=None):
        return self.answer


def test_pending_suggestions_return_the_answer_once():
    store = bridge.PendingSuggestions()
    p = FakePending()
    token = store.add(p)
    assert store.poll(token) is None  # not answered yet: kept
    p.answer = "S"
    assert store.poll(token) == "S"
    assert store.poll(token) is None  # removed after being returned


def test_pending_suggestions_expire():
    now = [100.0]
    store = bridge.PendingSuggestions(ttl=60.0, clock=lambda: now[0])
    token = store.add(FakePending("S"))
    other = store.add(FakePending())
    now[0] = 161.0
    assert store.poll(token) is None
    assert store.poll(other) is None
    assert len(store) == 0


def test_pending_suggestions_unknown_token():
    assert bridge.PendingSuggestions().poll("nope") is None
