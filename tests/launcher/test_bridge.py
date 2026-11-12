"""launcher.bridge: the pure glue behind the browser's launcher API (no webview needed)."""
import base64
import json
import os
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


def test_encoded_command_decodes_back():
    cmd = bridge.ps_command(["clauded", "-n", 'a "b" \\" ; $c `d  e ’f'])
    enc = bridge.encoded_command(cmd)
    assert set(enc) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")
    assert base64.b64decode(enc).decode("utf-16-le") == cmd


_TRICKY = ["-n", "it's", 'say "hi"', 'back\\"slash', "one; two", "$env:USERNAME", "back`tick", "two  spaces",
           "’quoted’", "a & b | c"]


@pytest.mark.skipif(not shutil.which("pwsh"), reason="pwsh not installed")
def test_encoded_command_round_trips_through_pwsh():
    """The launch argv as built (`console_argv`, which `wt_argv` ends with), run by real pwsh, hands every argument
    back unchanged. A `show` function stands in for the wrapper (defined by an outer -Command)."""
    argv = bridge.console_argv(["show", *_TRICKY], "pwsh")
    assert argv[:3] == ["pwsh", "-NoExit", "-EncodedCommand"]
    define = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
              "function show { foreach ($a in $args) { [Console]::Out.WriteLine('<' + $a + '>') } }; "
              "$cmd = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($env:ENC)); "
              "Invoke-Expression $cmd")
    out = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", define],
                         env={**os.environ, "ENC": argv[3]}, capture_output=True, timeout=60).stdout.decode("utf-8")
    assert out.splitlines() == [f"<{a}>" for a in _TRICKY]


@pytest.mark.skipif(not shutil.which("pwsh"), reason="pwsh not installed")
def test_encoded_command_runs_as_pwsh_argument():
    """pwsh itself decodes -EncodedCommand (a command needing no profile function)."""
    enc = bridge.encoded_command("[Console]::OutputEncoding = [Text.Encoding]::UTF8; " + bridge.ps_command(
        ["Write-Output", 'x "y" \\" ; $z `w  v']))
    out = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
                         capture_output=True, timeout=60).stdout.decode("utf-8")
    assert out.splitlines() == ['x "y" \\" ; $z `w  v']


# ---- terminal argv ----------------------------------------------------------------------------------------------

def _enc(argv):
    return bridge.encoded_command(bridge.CHILD_SESSION_PREFIX + bridge.ps_command(argv))


def test_launched_command_clears_the_child_session_marker_and_forces_persistence():
    argv = bridge.wt_argv(Path("C:/x"), ["clauded", "-n", "x"], "pwsh")
    cmd = base64.b64decode(argv[-1]).decode("utf-16-le")
    assert cmd == ("$env:CLAUDE_CODE_CHILD_SESSION=$null; $env:CLAUDE_CODE_FORCE_SESSION_PERSISTENCE='1'; "
                   "& 'clauded' '-n' 'x'")
    console = base64.b64decode(bridge.console_argv(["qwen"], "powershell")[-1]).decode("utf-16-le")
    assert console.startswith("$env:CLAUDE_CODE_CHILD_SESSION=$null; $env:CLAUDE_CODE_FORCE_SESSION_PERSISTENCE='1'; ")


def test_wt_argv_opens_a_tab_running_powershell(tmp_path):
    argv = bridge.wt_argv(tmp_path, ["clauded", "-n", "x; y"], "pwsh")
    assert argv == ["wt", "-w", "0", "nt", "-d", str(tmp_path), "pwsh", "-NoExit", "-EncodedCommand",
                    _enc(["clauded", "-n", "x; y"])]


def test_console_argv_has_no_wt():
    assert bridge.console_argv(["qwen", "-r", "s1"], "powershell") == [
        "powershell", "-NoExit", "-EncodedCommand", _enc(["qwen", "-r", "s1"])]


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
    assert bridge.children_listing(fake_env, idx, fake_env.root / "work" / "net")["kind_options"] == \
        ["project", "container"]
    top = bridge.children_listing(fake_env, idx, fake_env.root)
    assert top["start_here"] is False and top["rel"] == ""
    assert top["kind_options"] == ["organisational"]
    assert bridge.children_listing(fake_env, idx, fake_env.root / "cc")["default_wrapper"] == "claude"
    idx.close()


def test_children_listing_carries_the_remembered_defaults(fake_env):
    from launcher import defaults
    idx = _index(fake_env)
    r = fake_env.root
    (r / "work" / "wf").mkdir()
    assert bridge.children_listing(fake_env, idx, r / "work" / "wf")["defaults"] == {"wrapper": "clauded",
                                                                                    "github": "none"}
    assert bridge.children_listing(fake_env, idx, r / "work" / "net")["defaults"] == {"wrapper": "clauded",
                                                                                     "github": "personal"}
    assert bridge.children_listing(fake_env, idx, r / "cc")["defaults"] == {"wrapper": "claude",
                                                                           "github": "personal"}
    defaults.remember(fake_env, r / "work" / "net", "claudeb", "work")
    assert bridge.children_listing(fake_env, idx, r / "work" / "net")["defaults"] == {"wrapper": "claudeb",
                                                                                     "github": "work"}
    idx.close()


def test_children_listing_names_the_bucket(fake_env):
    """The dialog re-applies the defaults when the chosen folder's bucket changes, and only then."""
    idx = _index(fake_env)
    r = fake_env.root
    assert bridge.children_listing(fake_env, idx, r / "work" / "net")["bucket"] == "work\\net"
    assert bridge.children_listing(fake_env, idx, r / "home" / "ai")["bucket"] == "home"
    assert bridge.children_listing(fake_env, idx, r / "home")["bucket"] == "home"
    idx.close()


# ---- suggestion chips ---------------------------------------------------------------------------------------------

def test_chips_local_best_then_its_alternatives(fake_env):
    r = fake_env.root
    local = Suggestion(r / "work" / "halo", None, "medium", "matches halo", "history",
                       alternatives=[r / "work" / "net", r / "home" / "ai"])
    assert bridge.suggestion_chips(r, local) == [
        {"rel": "work/halo", "mark": "local", "source": "history", "reason": "matches halo", "new_folder": None,
         "needs_name": False},
        {"rel": "work/net", "mark": "alt", "source": None, "reason": "", "new_folder": None, "needs_name": False},
        {"rel": "home/ai", "mark": "alt", "source": None, "reason": "", "new_folder": None, "needs_name": False}]


def test_chips_model_answer_first_with_at_most_two_alternatives(fake_env):
    r = fake_env.root
    local = Suggestion(r / "work" / "halo", None, "medium", "halo", "history", alternatives=[r / "work" / "net"])
    model = Suggestion(r / "work" / "net", "forti_tool", "high", "vpn", "qwen",
                       alternatives=[r / "work" / "halo", r / "home" / "ai", r / "cc"])
    chips = bridge.suggestion_chips(r, local, model)
    assert [(c["rel"], c["mark"]) for c in chips] == [("work/net", "model"), ("work/halo", "alt"), ("home/ai", "alt")]
    assert chips[0]["new_folder"] == "forti_tool" and chips[0]["source"] == "qwen"


def test_chips_model_without_alternatives_falls_back_to_the_local_ranking(fake_env):
    r = fake_env.root
    local = Suggestion(r / "work" / "halo", None, "medium", "halo", "history", alternatives=[r / "work" / "net"])
    model = Suggestion(r / "work" / "net", None, "high", "vpn", "claude", needs_name=True)
    chips = bridge.suggestion_chips(r, local, model)
    assert [(c["rel"], c["mark"]) for c in chips] == [("work/net", "model"), ("work/halo", "alt")]  # no duplicate
    assert chips[0]["needs_name"] is True


def test_chips_none_for_a_root_answer(fake_env):
    root = Suggestion(fake_env.root, None, "low", "no suggestion; start at the root", "root")
    assert bridge.suggestion_chips(fake_env.root, root) == []
    assert bridge.suggestion_chips(fake_env.root, None) == []


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


def test_parse_new_accepts_github_none(fake_env):
    req = bridge.parse_new(fake_env.root, _payload(rel="home/ai", new_folder="llm", kind="project", github="none"))
    assert req.github == "none" and req.launches is True


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
    ({"new_folder": "x", "kind": "organisational"}, "kind"),  # work/net allows project or container
    ({"rel": "work/clients", "new_folder": "x", "kind": "project"}, "kind"),  # work/clients only allows client
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


def test_prepare_new_github_none_makes_no_gh_call_and_is_remembered(fake_env):
    runner = FakeRunner()
    result, launch = bridge.prepare_new(
        fake_env, _payload(rel="work/net", new_folder="export_x", kind="project", github="none"), runner=runner)
    assert result["ok"] and launch is not None
    assert ["git", "init", "-b", "main"] in runner.calls and not any(c[0] == "gh" for c in runner.calls)
    saved = json.loads((fake_env.data_dir / "defaults.json").read_text(encoding="utf-8"))
    assert saved == {"work\\net": {"wrapper": "clauded", "github": "none"}}


def test_prepare_new_remembers_choices_and_start_here_keeps_the_github_choice(fake_env):
    from launcher import defaults
    bridge.prepare_new(fake_env, _payload(rel="home/ai", new_folder="a", kind="project", wrapper="claudeb",
                                          github="work"), runner=FakeRunner())
    bridge.prepare_new(fake_env, _payload(rel="home/homelab", wrapper="claude6"), runner=FakeRunner())
    assert defaults.get(fake_env, fake_env.root / "home") == defaults.Defaults("claude6", "work")


def test_prepare_new_container_creates_and_does_not_launch(fake_env):
    result, launch = bridge.prepare_new(
        fake_env, _payload(rel="home", new_folder="box", kind="container", wrapper=None), runner=FakeRunner())
    assert launch is None
    assert result == {"ok": True, "path": str(fake_env.root / "home" / "box"), "rel": "home/box", "created": True,
                      "warnings": [], "launched": False}
    assert (fake_env.root / "home" / "box").is_dir()
    assert not (fake_env.data_dir / LAUNCHED_FILE).exists()
    assert not (fake_env.data_dir / "defaults.json").exists()  # nothing launched: nothing to remember


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


class FakeHandle(FakePending):
    """A `Pending` stand-in for `supersede`: records `cancel()`."""

    def __init__(self, local=None, answer=None):
        super().__init__(answer)
        self.local = local
        self.cancelled = False

    def cancel(self):
        self.cancelled = True
        self.answer = None


def test_supersede_cancels_the_previous_request_at_once():
    store = bridge.PendingSuggestions()
    first, second = FakeHandle(answer="A"), FakeHandle()
    t1 = store.supersede(first, "fix vpn")
    t2 = store.supersede(second, "fix vpn tunnel")
    assert first.cancelled and not second.cancelled
    assert store.poll(t1) is None
    second.answer = "B"
    assert store.poll(t2) == "B"


def test_in_effect_is_the_model_answer_once_handed_to_the_page_else_the_local_one(fake_env):
    r = fake_env.root
    local = Suggestion(r / "work" / "halo", None, "medium", "halo", "history")
    model = Suggestion(r / "work" / "net", None, "high", "vpn", "qwen")
    store = bridge.PendingSuggestions()
    handle = FakeHandle(local)
    token = store.supersede(handle, "  fix vpn ")
    assert store.local(token) is local and store.local("nope") is None
    assert store.in_effect("fix vpn") is local
    handle.answer = model
    assert store.in_effect("fix vpn") is local  # answered, but the page has not been shown it yet
    assert store.poll(token) is model
    assert store.in_effect("fix vpn") is model
    assert store.in_effect("another name") is None
    assert bridge.PendingSuggestions().in_effect("fix vpn") is None
    store.supersede(FakeHandle(local), "fix vpn")  # a new request for the same name starts from its local answer
    assert store.in_effect("fix vpn") is local


def test_suggest_has_no_deferred_model_path():
    """The dialog asks once per finished name, so the model stages start at once: the 1.5 s idle timer and
    `defer_models` are gone."""
    import inspect
    from launcher import suggest
    assert "defer_models" not in inspect.signature(suggest.suggest).parameters
    assert not hasattr(suggest.Pending, "release_after") and not hasattr(bridge, "MODEL_IDLE_S")
    assert bridge.SUGGEST_WAIT_MS == int((suggest.QWEN_TOTAL_S + suggest.CLAUDE_TOTAL_S) * 1000) + 1000


# ---- flow log and outcome -------------------------------------------------------------------------------------------

def _flow_events(env):
    path = env.data_dir / "flow.log"
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def _outcomes(env):
    path = env.data_dir / "suggest.log"
    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
    return [x for x in lines if x.get("stage") == "outcome"]


_FLOW_FIELDS = {"ts", "event", "folder", "kind", "filter", "step", "ui"}


def test_prepare_new_start_here_logs_launch_and_outcome(fake_env):
    r = fake_env.root
    store = bridge.PendingSuggestions()
    store.supersede(FakeHandle(Suggestion(r / "work" / "net", None, "medium", "vpn", "history")), "fix vpn")
    bridge.prepare_new(fake_env, _payload(), runner=FakeRunner(), suggestions=store)
    [launch] = _flow_events(fake_env)
    assert _FLOW_FIELDS <= set(launch)
    assert {k: launch[k] for k in ("event", "folder", "kind", "filter", "step", "ui", "folder_kind", "new_folder",
                                   "wrapper", "github", "cwd")} == {
        "event": "launch", "folder": "work\\net", "kind": None, "filter": "", "step": "done", "ui": "browser",
        "folder_kind": None, "new_folder": None, "wrapper": "clauded", "github": None, "cwd": "work\\net"}
    assert _outcomes(fake_env) == [{"ts": fake_env.now(), "stage": "outcome", "name": "fix vpn",
                                    "suggested": "work\\net", "suggested_source": "history", "chosen": "work\\net",
                                    "new_folder": None, "match": True}]


def test_prepare_new_project_logs_new_folder_kind_and_launch_like_the_terminal(fake_env):
    r = fake_env.root
    store = bridge.PendingSuggestions()
    handle = FakeHandle(Suggestion(r / "work" / "halo", None, "medium", "halo", "history"))
    token = store.supersede(handle, "fix vpn")
    handle.answer = Suggestion(r / "work" / "net", "vpn_fix", "high", "vpn", "qwen")
    assert store.poll(token) is handle.answer  # handed to the page: now the suggestion shown
    bridge.prepare_new(fake_env, _payload(new_folder="alert_review", kind="project", github="none", via="new_typed"),
                       runner=FakeRunner(), suggestions=store)
    events = _flow_events(fake_env)
    assert [e["event"] for e in events] == ["new-folder", "kind", "launch"]
    assert all(_FLOW_FIELDS <= set(e) and e["ui"] == "browser" for e in events)
    assert events[0]["kind"] == "new_typed" and events[0]["name"] == "alert_review" and events[0]["step"] == "folder_name"
    assert events[1]["value"] == "project" and events[1]["name"] == "alert_review" and events[1]["step"] == "kind"
    assert events[2]["github"] == "none" and events[2]["cwd"] == "work\\net\\alert_review"
    [out] = _outcomes(fake_env)
    assert out["suggested"] == "work\\net" and out["suggested_source"] == "qwen" and out["chosen"] == "work\\net"
    assert out["new_folder"] == "alert_review" and out["match"] is False  # suggested vpn_fix


def test_prepare_new_container_logs_the_new_folder_but_no_launch(fake_env):
    bridge.prepare_new(fake_env, _payload(rel="home", new_folder="box", kind="container", wrapper=None, via="bogus"),
                       runner=FakeRunner())
    events = _flow_events(fake_env)
    assert [e["event"] for e in events] == ["new-folder", "kind"]
    assert events[0]["kind"] is None  # an unknown `via` is not logged
    assert _outcomes(fake_env) == []


def test_prepare_new_without_a_suggestion_logs_no_suggested_folder(fake_env):
    store = bridge.PendingSuggestions()
    store.supersede(FakeHandle(Suggestion(fake_env.root / "work" / "net", None, "medium", "x", "history")), "other")
    bridge.prepare_new(fake_env, _payload(), runner=FakeRunner(), suggestions=store)
    [out] = _outcomes(fake_env)
    assert out["suggested"] is None and out["match"] is False


def test_prepare_new_launches_when_logging_fails(fake_env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("log broken")
    monkeypatch.setattr(bridge.flowlog, "log", boom)
    monkeypatch.setattr(bridge, "log_outcome", boom)
    result, launch = bridge.prepare_new(fake_env, _payload(new_folder="x1", kind="project"), runner=FakeRunner())
    assert result["ok"] is True and result["launched"] is True and launch is not None
    assert result["warnings"] == []


def test_log_cancel_mirrors_the_terminal_cancel_event(fake_env):
    bridge.log_cancel(fake_env, "work/net", "folder_name")
    bridge.log_cancel(fake_env, "work/nope", "bogus")
    first, second = _flow_events(fake_env)
    assert {k: first[k] for k in ("event", "folder", "kind", "filter", "step", "ui")} == {
        "event": "cancel", "folder": "work\\net", "kind": None, "filter": "", "step": "folder_name", "ui": "browser"}
    assert (second["folder"], second["step"]) == ("", "picker")  # unknown folder and step: root, picker


def test_log_start_mirrors_the_terminal_start_event(fake_env):
    r = fake_env.root
    bridge.log_start(fake_env, "fix vpn", Suggestion(r / "work" / "net", None, "medium", "vpn", "history", strong=True))
    bridge.log_start(fake_env, "zzz", Suggestion(r, None, "low", "root", "root"))
    first, second = _flow_events(fake_env)
    assert _FLOW_FIELDS <= set(first)
    assert (first["event"], first["folder"], first["step"], first["ui"], first["name"], first["suggested"],
            first["source"], first["strong"]) == ("start", "", "picker", "browser", "fix vpn", "work\\net",
                                                  "history", True)
    assert second["suggested"] is None and second["strong"] is False


def test_prepare_new_still_launches_with_a_warning_when_remembering_fails(fake_env, monkeypatch):
    def boom(*a, **k):
        raise PermissionError("sharing violation")
    monkeypatch.setattr(bridge.defaults, "remember", boom)
    result, launch = bridge.prepare_new(fake_env, _payload(), runner=FakeRunner())
    assert result["ok"] is True and result["launched"] is True and launch is not None
    assert any("could not remember the choices" in w for w in result["warnings"])


def test_prefix_is_pinned_literally_in_wt_and_console_commands(tmp_path):
    literal = "$env:CLAUDE_CODE_CHILD_SESSION=$null; $env:CLAUDE_CODE_FORCE_SESSION_PERSISTENCE='1'; "
    wt = bridge.wt_argv(tmp_path, ["clauded", "-n", "x"], "pwsh")
    assert base64.b64decode(wt[-1]).decode("utf-16-le") == literal + "& 'clauded' '-n' 'x'"
    console = bridge.console_argv(["qwen", "-r", "s1"], "powershell")
    assert base64.b64decode(console[-1]).decode("utf-16-le") == literal + "& 'qwen' '-r' 's1'"
