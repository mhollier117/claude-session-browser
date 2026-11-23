"""The browser's launcher API (`claude_sessions.Api`): Qwen rows in the session list, resume and new-session launches,
suggestions. Popen, the scaffold runner and the suggestion HTTP client are all faked; pywebview is never started."""
import base64
import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
pytest.importorskip("webview")
import claude_sessions as cs  # noqa: E402
from launcher import bridge  # noqa: E402
from launcher.index import Index, LAUNCHED_FILE  # noqa: E402
from launcher.scaffold import Result  # noqa: E402
from launcher_helpers import write_qwen_session  # noqa: E402

_CLAUDE_ROW = {"id": "c1", "auto_title": "claude one", "first_user": "hi", "cwd": "C:\\x", "mtime": 1_700_000_000.0,
               "user_msgs": 1, "assistant_msgs": 1, "total_msgs": 2, "project": "C--x"}


class FakeRunner:
    def __init__(self):
        self.calls = []

    def run(self, args, cwd, env=None, timeout=60, input=None):
        self.calls.append(list(args))
        return Result(1, "", "not run in tests")


def _enc(argv):
    return bridge.encoded_command(bridge.CHILD_SESSION_PREFIX + bridge.ps_command(argv))


def _settled(api, force=False):
    """`_sessions()` once the background index builder it started has finished (its rows show on the next call)."""
    api._sessions(force)
    thread = getattr(api, "_launcher_thread", None)
    if thread is not None:
        thread.join(10)
    return api._sessions()


def _q_user(text):
    return {"type": "user", "provenance": "real_user", "message": {"role": "user", "parts": [{"text": text}]}}


@pytest.fixture
def api(fake_env, monkeypatch):
    """An Api wired to `fake_env` through the CC_LAUNCHER_* variables, with every side effect captured."""
    monkeypatch.setenv("CC_LAUNCHER_ROOT", str(fake_env.root))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(fake_env.claude_home))
    monkeypatch.setenv("CC_LAUNCHER_QWEN_HOME", str(fake_env.qwen_home))
    monkeypatch.setenv("CC_LAUNCHER_DATA", str(fake_env.data_dir))
    popens = []
    monkeypatch.setattr(cs.subprocess, "Popen", lambda argv, **kw: popens.append((list(argv), kw)))
    monkeypatch.setattr(bridge, "pick_shell", lambda which=None: "pwsh")
    # the once-per-run profile read of the alias labels runs a real shell: none in tests (test_launch_records)
    monkeypatch.setattr(bridge, "profile_alias_labels", lambda *a, **k: {})
    runner = FakeRunner()
    monkeypatch.setattr(cs, "_LAUNCHER_RUNNER", runner)
    monkeypatch.setattr(cs, "_LAUNCHER_HTTP", lambda *a: (_ for _ in ()).throw(OSError("no network in tests")))
    monkeypatch.setattr(cs, "collect_sessions", lambda d: [dict(_CLAUDE_ROW)])
    a = cs.Api.__new__(cs.Api)
    a.settings, a.overrides, a._cache = {}, {}, None
    monkeypatch.setattr(a, "_projects_dir", lambda: str(fake_env.claude_home / "projects"))
    a.popens, a.runner = popens, runner
    yield a
    if getattr(a, "_launcher_index", None) is not None:
        a._launcher_index.close()


# ---- the session list ---------------------------------------------------------------------------------------------

def test_sessions_include_qwen_rows_with_tool(fake_env, api):
    ai = fake_env.root / "home" / "ai"
    write_qwen_session(fake_env, ai, "q1", [_q_user("hello qwen")])
    assert [r["id"] for r in api._sessions()] == ["c1"]  # the first call never waits for the index build
    rows = _settled(api)
    assert [(r["id"], r["tool"]) for r in rows] == [("c1", "claude"), ("q1", "qwencli")]
    q = rows[1]
    assert q["display_title"] == "hello qwen" and q["cwd"] == str(ai) and q["project"] == "ai"
    assert q["when"] == cs.fmt_time(q["mtime"]) and q["total_msgs"] == 1 and q["color"] == ""
    assert rows[0]["display_title"] == "claude one" and rows[0]["when"] == cs.fmt_time(_CLAUDE_ROW["mtime"])
    json.dumps(rows)
    assert [r["id"] for r in api._sessions()] == ["c1", "q1"]  # not appended twice


def test_sessions_refresh_the_index_only_when_forced(fake_env, api, monkeypatch):
    refreshes = []
    real = Index.refresh
    monkeypatch.setattr(Index, "refresh", lambda self, *a, **k: refreshes.append(1) or real(self, *a, **k))
    _settled(api)
    api._sessions()
    assert len(refreshes) == 1  # created and refreshed once
    assert api._launcher_thread is None
    write_qwen_session(fake_env, fake_env.root / "cc", "q2", [_q_user("new one")])
    assert "q2" not in [r["id"] for r in api._sessions()]
    assert "q2" in [r["id"] for r in _settled(api, force=True)]
    assert len(refreshes) == 2


def test_sessions_do_not_wait_for_a_busy_launcher(fake_env, api):
    """Another thread holds the launcher lock (a long build, or cc holding the sqlite file): the Claude rows and
    the cached Qwen rows come back at once, and the refresh lands once the lock is free."""
    write_qwen_session(fake_env, fake_env.root / "cc", "q1", [_q_user("x")])
    _settled(api)
    write_qwen_session(fake_env, fake_env.root / "cc", "q2", [_q_user("y")])
    held, release = threading.Event(), threading.Event()

    def hold():
        with api._launcher_lock:
            held.set()
            release.wait(10)
    holder = threading.Thread(target=hold)
    holder.start()
    held.wait(5)
    t0 = time.monotonic()
    rows = api._sessions(force=True)
    assert time.monotonic() - t0 < 1.0
    assert [r["id"] for r in rows] == ["c1", "q1"]
    release.set()
    holder.join()
    thread = api._launcher_thread
    if thread is not None:
        thread.join(10)
    assert sorted(r["id"] for r in api._sessions()) == ["c1", "q1", "q2"]


def test_sessions_keep_the_claude_list_when_the_launcher_fails(api, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("index exploded")
    monkeypatch.setattr(Index, "refresh", broken)
    logged = []
    monkeypatch.setattr(cs, "_launcher_log", logged.append)
    assert [r["id"] for r in _settled(api)] == ["c1"]
    assert logged and "index exploded" in logged[0]


def test_failed_index_build_is_retried_at_most_once_a_minute(api, monkeypatch):
    builds = []

    def broken(self, *a, **k):
        builds.append(1)
        raise RuntimeError("no index")
    monkeypatch.setattr(Index, "refresh", broken)
    monkeypatch.setattr(cs, "_launcher_log", lambda m: None)
    _settled(api)
    _settled(api, force=True)
    assert api.launcher_rows()["ok"] is False
    assert len(builds) == 1
    api._launcher_failed_at -= cs.Api._LAUNCHER_RETRY_S
    _settled(api)
    assert len(builds) == 2


def test_refresh_error_keeps_the_cached_qwen_rows(fake_env, api, monkeypatch):
    write_qwen_session(fake_env, fake_env.root / "cc", "q1", [_q_user("x")])
    _settled(api)

    def broken(*a, **k):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(Index, "refresh", broken)
    monkeypatch.setattr(cs, "_launcher_log", lambda m: None)
    assert [r["id"] for r in _settled(api, force=True)] == ["c1", "q1"]


def test_index_is_usable_from_another_thread(fake_env, api):
    """pywebview calls every js_api method on a thread of its own."""
    write_qwen_session(fake_env, fake_env.root / "cc", "q1", [_q_user("x")])
    _settled(api)
    out, errors = [], []

    def other():
        try:
            out.extend(api.launcher_rows())
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    t = threading.Thread(target=other)
    t.start()
    t.join()
    assert not errors and [r["sid"] for r in out] == ["q1"]


def test_plugin_sessions_stay_claude_only(fake_env, api):
    write_qwen_session(fake_env, fake_env.root / "cc", "q1", [_q_user("x")])
    _settled(api)
    host = cs.PluginHost(api, {"id": "p", "version": "1", "permissions": ["sessions"]})
    assert [s["id"] for s in host.sessions()] == ["c1"]


def test_launcher_methods_answer_errors_instead_of_raising(api, monkeypatch):
    def broken(self, *a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(cs.Api, "_launcher_state", broken)
    monkeypatch.setattr(cs, "_launcher_log", lambda m: None)
    for call in (lambda: api.launcher_rows(), lambda: api.launcher_children("x"),
                 lambda: api.launcher_kind_options("x"), lambda: api.launcher_suggest("x"),
                 lambda: api.launcher_suggest_poll("t"), lambda: api.launcher_new({}),
                 lambda: api.launcher_cancel("x", "picker")):
        assert call() == {"ok": False, "error": "RuntimeError: boom"}


# ---- resume -------------------------------------------------------------------------------------------------------

def test_resume_qwen_row_uses_qwen_r(fake_env, api):
    cwd = fake_env.root / "home" / "ai"
    assert api.resume("q1", str(cwd), "", "qwencli") == {"ok": True}
    [(argv, _kw)] = api.popens
    assert argv == ["wt", "-w", "0", "nt", "-d", str(cwd), "pwsh", "-NoExit", "-EncodedCommand",
                    _enc(["qwen", "-r", "q1"])]


def test_resume_qwen_rejects_a_bad_session_id(api):
    assert api.resume("q1; rm", "C:\\", "", "qwencli")["ok"] is False
    assert api.popens == []


def test_resume_claude_row_uses_the_clauded_alias_through_powershell(fake_env, api):
    """The resume button runs `clauded -r <sid>` in a PowerShell tab with the profile (where the alias lives),
    with the child-session marker cleared, in every area (cc included)."""
    for area in ("cc", "work"):
        api.popens.clear()
        cwd = fake_env.root / area
        assert api.resume("c1", str(cwd)) == {"ok": True}
        [(argv, _kw)] = api.popens
        assert argv == ["wt", "-w", "0", "nt", "-d", str(cwd), "pwsh", "-NoExit", "-EncodedCommand",
                        _enc(["clauded", "-r", "c1"])]
        command = base64.b64decode(argv[-1]).decode("utf-16-le")
        assert command.startswith("$env:CLAUDE_CODE_CHILD_SESSION=$null; $env:CLAUDE_CODE_FORCE_SESSION_PERSISTENCE='1'; ")


@pytest.mark.parametrize("last, used", [("claudeb", "claudeb"), ("claude6", "claude6"), ("claudew", "claudew"),
                                        ("claude", "clauded"), (None, "clauded")])
def test_resume_claude_keeps_the_account_wrapper_that_last_launched_it(fake_env, api, monkeypatch, last, used):
    """As the launcher's resume list: an account wrapper is kept, plain claude or none resumes with clauded."""
    from types import SimpleNamespace
    cwd = fake_env.root / "work"
    row = SimpleNamespace(store="claude", sid="c1", cwd=str(cwd), cwd_exists=True, wrapper=last, backend="claude")
    monkeypatch.setattr(api, "_launcher_idx", lambda refresh=False: SimpleNamespace(
        row=lambda store, sid: row if (store, sid) == ("claude", "c1") else None))
    assert api.resume("c1", str(cwd)) == {"ok": True}
    [(argv, _kw)] = api.popens
    assert argv[-1] == _enc([used, "-r", "c1"])


def test_resume_claude_still_resumes_with_clauded_when_the_index_fails(fake_env, api, monkeypatch):
    def boom(refresh=False):
        raise RuntimeError("the launcher index is unavailable (retried shortly)")
    monkeypatch.setattr(api, "_launcher_idx", boom)
    monkeypatch.setattr(cs, "_launcher_log", lambda m: None)
    cwd = fake_env.root / "work"
    assert api.resume("c1", str(cwd)) == {"ok": True}
    [(argv, _kw)] = api.popens
    assert argv[-1] == _enc(["clauded", "-r", "c1"])


def test_resume_claude_rejects_a_bad_session_id(api):
    assert api.resume("c1 & calc", "C:\\")["ok"] is False
    assert api.popens == []


def test_launch_falls_back_to_a_console_without_wt(fake_env, api, monkeypatch):
    calls = []

    def popen(argv, **kw):
        calls.append((list(argv), kw))
        if argv[0] == "wt":
            raise FileNotFoundError("wt")
    monkeypatch.setattr(cs.subprocess, "Popen", popen)
    cwd = fake_env.root / "cc"
    assert api.resume("q1", str(cwd), "", "qwencli") == {"ok": True}
    argv, kw = calls[1]
    assert argv == ["pwsh", "-NoExit", "-EncodedCommand", _enc(["qwen", "-r", "q1"])]
    assert kw["cwd"] == str(cwd) and kw["creationflags"] == cs.subprocess.CREATE_NEW_CONSOLE


# ---- new session --------------------------------------------------------------------------------------------------

def test_launcher_new_launches_wt_with_name_argv(fake_env, api):
    name = "it's a \"test\" $x; `y"
    res = api.launcher_new({"name": name, "rel": "work/net", "new_folder": None, "kind": None,
                            "wrapper": "clauded", "github": None})
    net = fake_env.root / "work" / "net"
    assert res == {"ok": True, "path": str(net), "rel": "work/net", "created": False, "warnings": [],
                   "launched": True}
    [(argv, _kw)] = api.popens
    assert argv[:9] == ["wt", "-w", "0", "nt", "-d", str(net), "pwsh", "-NoExit", "-EncodedCommand"]
    command = base64.b64decode(argv[9]).decode("utf-16-le")
    assert command.startswith(bridge.CHILD_SESSION_PREFIX + "& 'clauded' '-n' 'it''s a \"test\" $x; `y' '--session-id' '")
    sid = command.rsplit("'", 2)[1]
    launched = json.loads((fake_env.data_dir / LAUNCHED_FILE).read_text(encoding="utf-8"))
    assert launched == {f"claude:{sid}": "clauded"}


def test_launcher_new_project_runs_the_scaffold_through_the_hook(fake_env, api):
    res = api.launcher_new({"name": "llm work", "rel": "home/ai", "new_folder": "llm", "kind": "project",
                            "wrapper": "clauded", "github": None})
    assert res["ok"] and res["created"] and res["launched"]
    assert any("git init failed" in w for w in res["warnings"])  # the fake runner fails every command
    assert api.runner.calls[0] == ["git", "init", "-b", "main"]
    [(argv, _kw)] = api.popens
    assert argv[5] == str(fake_env.root / "home" / "ai" / "llm")
    command = base64.b64decode(argv[-1]).decode("utf-16-le")
    assert command.startswith(bridge.CHILD_SESSION_PREFIX)  # clears the child-session marker, forces persistence
    saved = json.loads((fake_env.data_dir / "defaults.json").read_text(encoding="utf-8"))
    assert saved == {"home": {"wrapper": "clauded", "github": "none"}}  # the next dialog preselects these
    assert api.launcher_children("home/ai")["defaults"] == {"wrapper": "clauded", "github": "none"}
    assert api.launcher_children("home/ai")["bucket"] == "home"


def test_launcher_new_container_creates_without_launching(fake_env, api):
    res = api.launcher_new({"name": "x", "rel": "home", "new_folder": "box", "kind": "container"})
    assert res["ok"] and res["created"] and not res["launched"] and res["rel"] == "home/box"
    assert api.popens == []


def test_launcher_new_invalid_payload(api):
    res = api.launcher_new({"name": "x", "rel": "work/net", "wrapper": "bash"})
    assert res["ok"] is False and "wrapper" in res["error"]
    assert api.popens == []


def test_launcher_new_reports_a_failed_launch(api, monkeypatch):
    def popen(argv, **kw):
        raise PermissionError("blocked")
    monkeypatch.setattr(cs.subprocess, "Popen", popen)
    res = api.launcher_new({"name": "x", "rel": "work/net", "wrapper": "clauded"})
    assert res["ok"] is False and res["launched"] is False and "blocked" in res["error"]


# ---- tree and rows ------------------------------------------------------------------------------------------------

def test_launcher_children_and_kind_options(fake_env, api):
    listing = api.launcher_children("home")
    assert [c["rel"] for c in listing["children"]] == ["home/ai", "home/homelab"]
    assert listing["start_here"] is True and listing["kind_options"] == ["project", "container"]
    assert api.launcher_children("")["start_here"] is False
    assert api.launcher_children("nope")["ok"] is False
    assert api.launcher_kind_options("work/clients") == ["client"]
    assert api.launcher_kind_options("nope") == []


def test_launcher_rows_carry_tool(fake_env, api):
    write_qwen_session(fake_env, fake_env.root / "cc", "q1", [_q_user("x")])
    [row] = api.launcher_rows()
    assert row["tool"] == "qwen" and row["sid"] == "q1"


# ---- suggestions --------------------------------------------------------------------------------------------------

def _qwen_settings(env):
    (env.qwen_home / "settings.json").write_text(json.dumps({
        "model": {"name": "m", "baseUrl": "http://qwen.test:8000/v1"},
        "modelProviders": {"openai": [{"id": "m", "envKey": "K"}]},
        "env": {"K": "secret"}}), encoding="utf-8")


def _choose(message, rel):
    """The v2 answer picking candidate `rel` from the numbered user message."""
    import re
    n = int(re.search(rf"^(\d+)\. \w+\s+{re.escape(rel)}(?:  |$)", message, re.M).group(1))
    return json.dumps({"choice": n, "new_folder": None, "confidence": "high", "reason": "tickets"})


def test_launcher_suggest_returns_local_then_poll(fake_env, api, monkeypatch):
    _qwen_settings(fake_env)
    http_calls = []

    def http(method, url, headers, body, timeout):
        http_calls.append((method, url))
        if method == "GET":
            return 200, {"data": [{"id": "m-current"}]}
        return 200, {"choices": [{"message": {"content": _choose(body["messages"][1]["content"], "work\\halo")}}]}
    monkeypatch.setattr(cs, "_LAUNCHER_HTTP", http)
    res = api.launcher_suggest("fix the vpn tunnel")
    assert res["local"] == {"path": "", "new_folder": None, "confidence": "low",
                            "reason": "no suggestion; start at the root", "source": "root"}  # no history yet
    assert res["chips"] == [] and res["wait_ms"] == bridge.SUGGEST_WAIT_MS
    token = res["token"]
    got, deadline = None, time.monotonic() + 10
    while got is None and time.monotonic() < deadline:
        got = api.launcher_suggest_poll(token)
        time.sleep(0.02)
    chips = got.pop("chips")
    assert got == {"path": "work/halo", "new_folder": None, "confidence": "high", "reason": "tickets",
                   "source": "qwen"}
    assert chips[0] == {"rel": "work/halo", "mark": "model", "source": "qwen", "reason": "tickets",
                        "new_folder": None, "needs_name": True}  # a place with no sessions of its own
    assert len(chips) <= 3 and all(c["mark"] == "alt" for c in chips[1:])
    assert api.launcher_suggest_poll(token) is None  # handed out once
    assert http_calls[0] == ("GET", "http://qwen.test:8000/v1/models")


def test_launcher_suggest_starts_the_models_at_once_and_a_newer_name_supersedes(fake_env, api, monkeypatch):
    """No idle timer any more: each finished name asks the model at once; only the latest answer is handed out."""
    _qwen_settings(fake_env)
    posts = []

    def http(method, url, headers, body, timeout):
        if method == "GET":
            return 200, {"data": [{"id": "m-current"}]}
        posts.append(body["messages"][1]["content"])
        return 200, {"choices": [{"message": {"content": json.dumps({"choice": 1})}}]}
    monkeypatch.setattr(cs, "_LAUNCHER_HTTP", http)
    first = api.launcher_suggest("vpn")["token"]
    deadline = time.monotonic() + 10
    while not posts and time.monotonic() < deadline:
        time.sleep(0.02)
    assert posts and "vpn" in posts[0]  # started without waiting for an idle time
    last = api.launcher_suggest("vpn tunnel")["token"]
    got, deadline = None, time.monotonic() + 10
    while got is None and time.monotonic() < deadline:
        got = api.launcher_suggest_poll(last)
        time.sleep(0.02)
    assert got["source"] == "qwen"
    assert api.launcher_suggest_poll(first) is None  # superseded: cancelled, never handed out


def test_launcher_suggest_logs_the_start_event(fake_env, api):
    api.launcher_suggest("fix the vpn tunnel")
    lines = [json.loads(x) for x in (fake_env.data_dir / "flow.log").read_text(encoding="utf-8").splitlines()]
    assert [(e["event"], e["ui"], e["name"]) for e in lines] == [("start", "browser", "fix the vpn tunnel")]


def test_launcher_new_logs_the_outcome_against_the_suggestion_shown(fake_env, api):
    api.launcher_suggest("fix vpn")
    res = api.launcher_new({"name": "fix vpn", "rel": "home/ai", "new_folder": "vpn", "kind": "project",
                            "wrapper": "clauded", "github": "none", "via": "new_typed"})
    assert res["ok"] and res["launched"]
    events = [json.loads(x) for x in (fake_env.data_dir / "flow.log").read_text(encoding="utf-8").splitlines()]
    assert [e["event"] for e in events] == ["start", "new-folder", "kind", "launch"]
    outcomes = [json.loads(x) for x in (fake_env.data_dir / "suggest.log").read_text(encoding="utf-8").splitlines()
                if '"outcome"' in x]
    assert len(outcomes) == 1 and outcomes[0]["name"] == "fix vpn" and outcomes[0]["chosen"] == "home\\ai"
    assert outcomes[0]["new_folder"] == "vpn"


def test_launcher_suggest_with_no_name_asks_nothing(fake_env, api, monkeypatch):
    def no_suggest(*a, **k):
        raise AssertionError("suggest() must not run for an empty name")
    monkeypatch.setattr("launcher.suggest.suggest", no_suggest)
    for name in ("", "   ", None):
        res = api.launcher_suggest(name)
        assert res["token"] is None and res["chips"] == [] and res["wait_ms"] == 0
        assert res["local"]["path"] == "" and res["local"]["source"] == "root"
    assert not (fake_env.data_dir / "flow.log").exists()  # no start event either


def test_launcher_cancel_logs_a_browser_cancel(fake_env, api):
    assert api.launcher_cancel("work/net", "picker") == {"ok": True}
    [line] = (fake_env.data_dir / "flow.log").read_text(encoding="utf-8").splitlines()
    e = json.loads(line)
    assert (e["event"], e["folder"], e["step"], e["ui"]) == ("cancel", "work\\net", "picker", "browser")


def test_launcher_new_created_folder_then_failed_launch_reports_created(fake_env, api, monkeypatch):
    def popen(argv, **kw):
        raise PermissionError("blocked")
    monkeypatch.setattr(cs.subprocess, "Popen", popen)
    res = api.launcher_new({"name": "x", "rel": "home/ai", "new_folder": "fresh", "kind": "project",
                            "wrapper": "clauded", "github": "none"})
    assert res["ok"] is False and res["launched"] is False and "blocked" in res["error"]
    assert res["created"] is True and res["rel"] == "home/ai/fresh"  # the page walks into the new folder
    assert (fake_env.root / "home" / "ai" / "fresh").is_dir()


def test_launcher_new_launch_survives_a_broken_log(fake_env, api, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(bridge.flowlog, "log", boom)
    monkeypatch.setattr(bridge, "log_outcome", boom)
    res = api.launcher_new({"name": "x", "rel": "work/net", "wrapper": "clauded"})
    assert res["ok"] is True and res["launched"] is True and len(api.popens) == 1


def test_launcher_suggest_poll_unknown_token(api):
    assert api.launcher_suggest_poll("nope") is None


# ---- the dialog's page contract (no JS runner: pin the script's wiring) ----------------------------------------------

@pytest.fixture(scope="module")
def page():
    return cs.build_html()


def _js_fn(page, name):
    """The source of the dialog's JS function `name` (up to the next top-level function or comment)."""
    import re
    m = re.search(rf"^(?:async )?function {name}\(.*?(?=^(?:async )?function |^// |^/\*)", page, re.M | re.S)
    assert m, name
    return m.group(0)


def test_page_asks_once_per_finished_name_not_while_typing(page):
    assert "debT" not in page and "setTimeout(()=>lnSuggest" not in page  # the 400 ms typing pause is gone
    assert "addEventListener('input',lnOnName)" in page
    assert "addEventListener('keydown',e=>{ if(e.key==='Enter') lnNameDone(); })" in page
    assert "addEventListener('blur',lnNameDone)" in page
    assert "lnSuggest" not in _js_fn(page, "lnOnName")
    done = _js_fn(page, "lnNameDone")
    assert "name===LN.suggName" in done and "lnSuggest(name)" in done  # Enter then blur asks once


def test_page_chips_open_the_columns_and_never_move_or_launch_by_themselves(page):
    render = _js_fn(page, "lnRenderSugg")
    assert "LN.chips.map" in render and "lnUseChip(${i})" in render and "'★ '" in render and "'≈ '" in render
    suggest = _js_fn(page, "lnSuggest")
    assert "LN.chips=r.chips" in suggest and "LN.chips=s.chips" in suggest
    assert "lnGoto" not in suggest and "lnPick" not in suggest  # a suggestion is only ever a chip
    use = _js_fn(page, "lnUseChip")
    assert "lnGoto(c.rel)" in use and "lnOpenNewFolder(LN.cols.length-1, c.new_folder)" in use
    assert "c.needs_name" in use and "lnSubmit" not in use and "launcher_new" not in use


def test_page_typed_folder_name_wins_over_the_session_slug(page):
    from launcher.tree import slug
    assert "replace(/[^a-z0-9._-]+/g,'_').replace(/^_+|_+$/g,'')" in _js_fn(page, "lnSlug")  # = tree.slug
    assert slug("Alert Review!") == "alert_review"
    assert "lnSlug(nf.touched ? nf.name : lnName())" in _js_fn(page, "lnNfFolder")
    typed = _js_fn(page, "lnNfName")
    assert "LN.nf.touched=!!v.trim()" in typed and "'new_typed'" in typed
    create = _js_fn(page, "lnCreate")
    assert "lnNfFolder()" in create and "via: nf.via" in create


def test_page_preselects_the_bucket_defaults_and_offers_github_none(page):
    apply = _js_fn(page, "lnApplyDefaults")
    assert "c.bucket!==LN.defBucket" in apply and "LN.wrapperSet=false" in apply and "LN.githubSet=false" in apply
    assert "c.defaults" in apply and "!LN.githubSet" in apply and "!LN.wrapperSet" in apply
    assert "LN.githubSet=true" in _js_fn(page, "lnSetGithub")
    nf = _js_fn(page, "lnRenderNf")
    assert "['none', t('keins'), t('kein GitHub-Repo, nur git init')]" in nf
    assert "github: project ? LN.github : null" in _js_fn(page, "lnCreate")
    assert "lnApplyDefaultWrapper" not in page


def test_page_a_failed_suggestion_can_be_asked_again(page):
    suggest = _js_fn(page, "lnSuggest")
    failed = suggest[suggest.index("if(!r || r.ok===false || !r.local){"):]
    assert failed.index("LN.suggName='';") < failed.index("return;")


def test_page_a_typed_name_that_slugs_to_nothing_never_falls_back(page):
    norm = _js_fn(page, "lnNfNormalise")
    assert "if(!LN.nf || !LN.nf.touched) return;" in norm
    assert "if(!folder){ lnSetMsg(t('Bitte gib einen Ordnernamen ein.'), 'err'); return; }" in norm
    assert 'onchange="lnNfNormalise(this)"' in page
    assert "if(!folder){ lnSetMsg(t('Bitte gib einen Ordnernamen ein.'), 'err'); return; }" in _js_fn(page, "lnCreate")


def test_page_logs_cancel_only_when_closed_without_a_launch(page):
    close = _js_fn(page, "lnClose")
    assert close.startswith("function lnClose(launched){") and "if(!launched && lnIsOpen())" in close
    assert "api.launcher_cancel(" in close
    assert "lnClose(true);" in _js_fn(page, "lnSubmit")
    assert "keywords" not in _js_fn(page, "lnSourceName")


def test_page_new_strings_are_translated(page):
    import i18n
    for key in ("keins", "kein GitHub-Repo, nur git init", "neuer Ordner…"):
        assert f"t('{key}')" in page and key in i18n.TRANSLATIONS["en"]


# ---- the type of each row and resuming other backends -----------------------------------------------------------

def _c_user(text, cwd):
    return {"type": "user", "cwd": str(cwd), "message": {"role": "user", "content": text}}


def _c_reply(model):
    return {"type": "assistant", "message": {"role": "assistant", "model": model,
                                             "content": [{"type": "text", "text": "ok"}]}}


def _claude_rows(api, monkeypatch, rows):
    """The app's own parser returns these Claude rows (ids only matter)."""
    monkeypatch.setattr(cs, "collect_sessions", lambda d: [dict(_CLAUDE_ROW, id=i) for i in rows])


def test_session_list_types_come_from_the_launcher_index(fake_env, api, monkeypatch):
    from launcher_helpers import write_claude_session
    cwd = fake_env.root / "home" / "ai"
    for sid, model in (("c1", "claude-opus-4-1"), ("g1", "glm-5.3[1m]"), ("k1", "qwen3-vl-current")):
        write_claude_session(fake_env, cwd, sid, [_c_user("hi", cwd), _c_reply(model)])
    write_qwen_session(fake_env, cwd, "q1", [_q_user("hello qwen")])
    _claude_rows(api, monkeypatch, ["c1", "g1", "k1", "n1"])  # n1: not in the index (yet)
    assert {r["id"]: r["tool"] for r in api._sessions()} == {"c1": "claude", "g1": "claude", "k1": "claude",
                                                            "n1": "claude"}  # before the index read: claude
    types = {r["id"]: r["tool"] for r in _settled(api)}
    assert types == {"c1": "claude", "g1": "glm", "k1": "qwen", "n1": "claude", "q1": "qwencli"}
    host = cs.PluginHost(api, {"id": "p", "version": "1", "permissions": ["sessions"]})
    assert [s["id"] for s in host.sessions()] == ["c1", "g1", "k1", "n1"]  # every Claude backend, no Qwen Code


def test_session_list_types_are_claude_when_the_index_is_unavailable(fake_env, api, monkeypatch):
    from launcher_helpers import write_claude_session
    cwd = fake_env.root / "home" / "ai"
    write_claude_session(fake_env, cwd, "g1", [_c_user("hi", cwd), _c_reply("glm-5.3")])
    _claude_rows(api, monkeypatch, ["g1"])

    def broken(*a, **k):
        raise RuntimeError("index exploded")
    monkeypatch.setattr(Index, "refresh", broken)
    monkeypatch.setattr(cs, "_launcher_log", lambda m: None)
    assert [(r["id"], r["tool"]) for r in _settled(api)] == [("g1", "claude")]


@pytest.mark.parametrize("backend, last, wrapper_part", [
    ("glm", None, ["claudez", "--dangerously-skip-permissions"]),
    ("glm", "claude6", ["claudez", "--dangerously-skip-permissions"]),
    ("qwen", "claudeb", ["claudeq", "--dangerously-skip-permissions"]),
    ("claude", None, ["clauded"]),
    ("claude", "claudew", ["claudew"]),
])
def test_resume_claude_row_on_each_backend(fake_env, api, monkeypatch, backend, last, wrapper_part):
    """A glm or qwen row is a Claude session: the page passes its type as `tool` and it still resumes through
    the Claude path, where the index row picks claudez / claudeq (bypass mode: the app has no ask-permissions
    resume)."""
    from types import SimpleNamespace
    cwd = fake_env.root / "work"
    row = SimpleNamespace(store="claude", sid="c1", cwd=str(cwd), cwd_exists=True, wrapper=last, backend=backend)
    monkeypatch.setattr(api, "_launcher_idx", lambda refresh=False: SimpleNamespace(
        row=lambda store, sid: row if (store, sid) == ("claude", "c1") else None))
    tool = {"glm": "glm", "qwen": "qwen", "claude": "claude"}[backend]
    assert api.resume("c1", str(cwd), "", tool) == {"ok": True}
    [(argv, _kw)] = api.popens
    assert argv[-1] == _enc([*wrapper_part, "-r", "c1"])


def test_resume_glm_row_from_a_real_index(fake_env, api):
    from launcher_helpers import write_claude_session
    cwd = fake_env.root / "home" / "ai"
    write_claude_session(fake_env, cwd, "g1", [_c_user("hi", cwd), _c_reply("glm-5.3-flash")])
    assert api.resume("g1", str(cwd), "", "glm") == {"ok": True}
    [(argv, _kw)] = api.popens
    assert argv[-1] == _enc(["claudez", "--dangerously-skip-permissions", "-r", "g1"])


def test_page_types_badges_and_qwen_code_checks(page):
    assert "const TOOL_STYLED = ['claude','qwen','glm','qwencli'];" in page
    # any alias name is a type (claudek, a new one): only its shape is checked, no list of known types
    assert "/^[A-Za-z0-9_-]{1,48}$/.test(s.tool) ? s.tool : 'claude'" in _js_fn(page, "toolOf")
    assert "TOOL_STYLED" not in _js_fn(page, "toolOf")
    for cls in ("qwencli", "qwen", "glm", "alias"):
        assert f".tool-badge.{cls}{{" in page
    badge = _js_fn(page, "toolBadge")
    assert "tool-badge alias" in badge and "--th:${toolHue(k)}" in badge and "esc(k)" in badge
    assert "toolTypes()" in _js_fn(page, "renderToolFilter")
    # a type and its ...cli twin (qwen, qwencli) share one button; a sub-choice narrows it when both are listed
    assert "k.endsWith('cli') ? k.slice(0,-3) : k" in _js_fn(page, "toolGroupOf")
    assert "toolGroupOf(toolOf(s))" in _js_fn(page, "toolTypes")
    match = _js_fn(page, "toolMatch")
    assert "TOOL_SUB==='claude') return k===TOOL_FILTER" in match and "k===TOOL_FILTER+'cli'" in match
    render_ = _js_fn(page, "renderToolFilter")
    assert "toolGroupHasBoth(TOOL_FILTER)" in render_ and "setToolSub(" in render_
    assert "TOOL_SUB='both'" in _js_fn(page, "setToolFilter")
    assert "if(!toolMatch(s)) return false;" in page
    assert "${toolBadge(s)}" in page
    assert "toolOf(s)==='qwencli'" in _js_fn(page, "openColor") and "toolOf(s)==='qwencli'" in _js_fn(page, "openRename")
    assert "toolOf(s)==='qwen'" not in page  # "qwen" is now Claude Code on a Qwen model, not Qwen Code
    assert "api.resume(s.id,s.cwd,s.project||'',toolOf(s))" in page
    assert "s.tool!=='claude'" in _js_fn(page, "repollQwen")


def test_claude_is_never_launched_through_cmd(page):
    assert not hasattr(cs, "resume_session")
    assert "claude_cmd" not in page and "update_setting('terminal'" not in page
    assert "claude_cmd" not in cs.DEFAULT_SETTINGS and "terminal" not in cs.DEFAULT_SETTINGS
    import inspect
    source = inspect.getsource(cs)
    assert '"--resume"' not in source and '"/k"' not in source  # no `cmd /k claude --resume` argv left