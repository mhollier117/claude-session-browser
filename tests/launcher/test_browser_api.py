"""The browser's launcher API (`claude_sessions.Api`): Qwen rows in the session list, resume and new-session launches,
suggestions. Popen, the scaffold runner and the suggestion HTTP client are all faked; pywebview is never started."""
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
    rows = api._sessions()
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
    api._sessions()
    api._sessions()
    assert len(refreshes) == 1  # created and refreshed once
    write_qwen_session(fake_env, fake_env.root / "cc", "q2", [_q_user("new one")])
    assert "q2" not in [r["id"] for r in api._sessions()]
    assert "q2" in [r["id"] for r in api._sessions(force=True)]
    assert len(refreshes) == 2


def test_sessions_keep_the_claude_list_when_the_launcher_fails(api, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("index exploded")
    monkeypatch.setattr(Index, "refresh", broken)
    logged = []
    monkeypatch.setattr(cs, "_launcher_log", logged.append)
    assert [r["id"] for r in api._sessions()] == ["c1"]
    assert logged and "index exploded" in logged[0]


def test_index_is_usable_from_another_thread(fake_env, api):
    """pywebview calls every js_api method on a thread of its own."""
    write_qwen_session(fake_env, fake_env.root / "cc", "q1", [_q_user("x")])
    api._sessions()
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


# ---- resume -------------------------------------------------------------------------------------------------------

def test_resume_qwen_row_uses_qwen_r(fake_env, api):
    cwd = fake_env.root / "home" / "ai"
    assert api.resume("q1", str(cwd), "", "qwen") == {"ok": True}
    [(argv, _kw)] = api.popens
    assert argv == ["wt", "-w", "0", "nt", "-d", str(cwd), "pwsh", "-NoExit", "-Command", "& 'qwen' '-r' 'q1'"]


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
    assert argv == ["pwsh", "-NoExit", "-Command", "& 'qwen' '-r' 'q1'"]
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
    assert argv[:9] == ["wt", "-w", "0", "nt", "-d", str(net), "pwsh", "-NoExit", "-Command"]
    command = argv[9]
    assert command.startswith("& 'clauded' '-n' 'it''s a \"test\" $x\\; `y' '--session-id' '")
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


def test_launcher_suggest_returns_local_then_poll(fake_env, api, monkeypatch):
    _qwen_settings(fake_env)
    answer = json.dumps({"path": "work/halo", "new_folder": None, "confidence": "high", "reason": "tickets"})
    http_calls = []

    def http(method, url, headers, body, timeout):
        http_calls.append((method, url))
        if method == "GET":
            return 200, {"data": [{"id": "m-current"}]}
        return 200, {"choices": [{"message": {"content": answer}}]}
    monkeypatch.setattr(cs, "_LAUNCHER_HTTP", http)
    res = api.launcher_suggest("fix the vpn tunnel")
    assert res["local"] == {"path": "work/net", "new_folder": None, "confidence": "low",
                            "reason": "name mentions vpn", "source": "keywords"}
    token = res["token"]
    got, deadline = None, time.monotonic() + 10
    while got is None and time.monotonic() < deadline:
        got = api.launcher_suggest_poll(token)
        time.sleep(0.02)
    assert got == {"path": "work/halo", "new_folder": None, "confidence": "high", "reason": "tickets",
                   "source": "qwen"}
    assert api.launcher_suggest_poll(token) is None  # handed out once
    assert http_calls[0] == ("GET", "http://qwen.test:8000/v1/models")


def test_launcher_suggest_poll_unknown_token(api):
    assert api.launcher_suggest_poll("nope") is None
