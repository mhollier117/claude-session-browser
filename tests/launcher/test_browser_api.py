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
    assert [(r["id"], r["tool"]) for r in rows] == [("c1", "claude"), ("q1", "qwen")]
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
                 lambda: api.launcher_suggest_poll("t"), lambda: api.launcher_new({})):
        assert call() == {"ok": False, "error": "RuntimeError: boom"}


# ---- resume -------------------------------------------------------------------------------------------------------

def test_resume_qwen_row_uses_qwen_r(fake_env, api):
    cwd = fake_env.root / "home" / "ai"
    assert api.resume("q1", str(cwd), "", "qwen") == {"ok": True}
    [(argv, _kw)] = api.popens
    assert argv == ["wt", "-w", "0", "nt", "-d", str(cwd), "pwsh", "-NoExit", "-EncodedCommand",
                    _enc(["qwen", "-r", "q1"])]


def test_resume_qwen_rejects_a_bad_session_id(api):
    assert api.resume("q1; rm", "C:\\", "", "qwen")["ok"] is False
    assert api.popens == []


def test_resume_claude_row_is_unchanged(fake_env, api):
    cwd = fake_env.root / "cc"
    assert api.resume("c1", str(cwd)) == {"ok": True}
    [(argv, kw)] = api.popens
    assert argv == ["wt", "-d", str(cwd), "cmd", "/k", "claude", "--resume", "c1"]
    assert kw["env"]["CLAUDE_CODE_FORCE_SESSION_PERSIST"] == "1"


def test_launch_falls_back_to_a_console_without_wt(fake_env, api, monkeypatch):
    calls = []

    def popen(argv, **kw):
        calls.append((list(argv), kw))
        if argv[0] == "wt":
            raise FileNotFoundError("wt")
    monkeypatch.setattr(cs.subprocess, "Popen", popen)
    cwd = fake_env.root / "cc"
    assert api.resume("q1", str(cwd), "", "qwen") == {"ok": True}
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
    token = res["token"]
    got, deadline = None, time.monotonic() + 10
    while got is None and time.monotonic() < deadline:
        got = api.launcher_suggest_poll(token)
        time.sleep(0.02)
    assert got == {"path": "work/halo", "new_folder": None, "confidence": "high", "reason": "tickets",
                   "source": "qwen"}
    assert api.launcher_suggest_poll(token) is None  # handed out once
    assert http_calls[0] == ("GET", "http://qwen.test:8000/v1/models")


def test_launcher_suggest_runs_the_models_only_for_the_latest_name(fake_env, api, monkeypatch):
    _qwen_settings(fake_env)
    monkeypatch.setattr(bridge, "MODEL_IDLE_S", 0.3)
    posts = []

    def http(method, url, headers, body, timeout):
        if method == "GET":
            return 200, {"data": [{"id": "m-current"}]}
        posts.append(body["messages"][1]["content"])
        return 200, {"choices": [{"message": {"content": json.dumps({"choice": 1})}}]}
    monkeypatch.setattr(cs, "_LAUNCHER_HTTP", http)
    tokens = [api.launcher_suggest(n)["token"] for n in ("vpn", "vpn tun", "vpn tunnel")]
    assert posts == []  # nothing before the name has been idle
    got, deadline = None, time.monotonic() + 10
    while got is None and time.monotonic() < deadline:
        got = api.launcher_suggest_poll(tokens[-1])
        time.sleep(0.02)
    assert got["source"] == "qwen"
    time.sleep(0.5)  # a superseded request would have released by now
    assert len(posts) == 1 and "vpn tunnel" in posts[0]
    assert api.launcher_suggest_poll(tokens[0]) is None


def test_launcher_suggest_poll_unknown_token(api):
    assert api.launcher_suggest_poll("nope") is None
