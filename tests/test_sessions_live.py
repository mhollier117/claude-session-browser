"""sessions_live: open-session detection from Claude Code's session registry.

Builds a fake ~/.claude (sessions/ + projects/) in a temp folder; process
liveness is injected, so no real processes or sessions are involved.
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sessions_live as sl  # noqa: E402

NOW = 1_800_000_000.0


class World:
    def __init__(self, root: Path):
        self.root = root
        (root / "sessions").mkdir()
        (root / "projects").mkdir()
        self.started = {}            # pid -> FILETIME the "OS" reports

    def session(self, pid, sid, status="idle", age=10, kind="interactive", cwd="C:\\work\\proj",
                name="proj-12", name_source="derived", proc_start=None, waiting_for=None):
        self.started.setdefault(pid, 1000 + pid)
        rec = {"pid": pid, "sessionId": sid, "cwd": cwd, "kind": kind, "entrypoint": "cli",
               "procStart": str(proc_start if proc_start is not None else self.started[pid]),
               "name": name, "nameSource": name_source, "status": status,
               "statusUpdatedAt": int((NOW - age) * 1000), "updatedAt": int((NOW - age) * 1000)}
        if waiting_for:
            rec["waitingFor"] = waiting_for
        (self.root / "sessions" / f"{pid}.json").write_text(json.dumps(rec))
        (self.root / "sessions" / f"{pid}.abc123.key").write_text("SECRET-NEVER-READ")
        return rec

    def transcript(self, sid, *entries, folder="C--work-proj"):
        d = self.root / "projects" / folder
        d.mkdir(exist_ok=True)
        p = d / f"{sid}.jsonl"
        with open(p, "a", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
        return p

    def live(self):
        return sl.LiveSessions(str(self.root), clock=lambda: NOW,
                               proc_start=lambda pid: self.started.get(pid))


def ai(t):
    return {"type": "ai-title", "aiTitle": t, "sessionId": "x"}


def custom(t):
    return {"type": "custom-title", "customTitle": t, "sessionId": "x"}


def assistant(*blocks, **extra):
    return dict({"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}, **extra)


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


def by_id(snap):
    return {s["session_id"]: s for s in snap}


def test_no_registry_means_none(tmp_path):
    assert sl.LiveSessions(str(tmp_path), proc_start=lambda p: 1).snapshot() is None


def test_states_from_status(w):
    w.session(1, "need", status="waiting", waiting_for="input needed")
    w.session(2, "work", status="busy")
    w.session(3, "shel", status="shell")
    w.session(4, "done", status="idle", age=60)
    w.session(5, "idle", status="idle", age=600)
    w.session(6, "slep", status="idle", age=5000)
    s = by_id(w.live().snapshot())
    assert {k: v["state"] for k, v in s.items()} == {
        "need": "needs_you", "work": "working", "shel": "working",
        "done": "done", "idle": "idle", "slep": "sleep"}
    assert s["need"]["waiting_for"] == "input needed"


def test_thinking_and_limit_come_from_the_transcript_tail(w):
    w.session(1, "think", status="busy")
    w.transcript("think", assistant({"type": "thinking", "thinking": "..."}))
    w.session(2, "limit", status="idle", age=30)
    w.transcript("limit", assistant({"type": "text", "text": "You've hit your limit"},
                                    isApiErrorMessage=True, apiErrorStatus=429, error="rate_limit"))
    s = by_id(w.live().snapshot())
    assert s["think"]["state"] == "thinking" and s["limit"]["state"] == "limit"


def test_closed_crashed_and_reused_pids_are_ignored(w):
    w.session(1, "alive", status="busy")
    w.session(2, "gone", status="busy")
    del w.started[2]                                  # process no longer exists
    w.session(3, "reused", status="busy", proc_start=999)   # PID now belongs to another program
    w.session(4, "bg", status="busy", kind="bg")
    assert [s["session_id"] for s in w.live().snapshot()] == ["alive"]


def test_most_urgent_first_then_most_recent(w):
    w.session(1, "old-work", status="busy", age=50)
    w.session(2, "new-work", status="busy", age=5)
    w.session(3, "needs", status="waiting", age=300)
    w.session(4, "done", status="idle", age=20)
    order = [s["session_id"] for s in w.live().snapshot()]
    assert order == ["needs", "new-work", "old-work", "done"]


def test_name_priority_custom_then_ai_then_registry_then_folder(w):
    w.session(1, "a", cwd="C:\\x\\alpha")
    w.transcript("a", ai("Auto title"), custom("My name"), ai("Newer auto title"))
    w.session(2, "b", cwd="C:\\x\\beta")
    w.transcript("b", ai("Only auto"), folder="C--x-beta")
    w.session(3, "c", name="gamma-17", cwd="C:\\x\\gamma")
    w.session(4, "d", name="", cwd="C:\\x\\delta")
    s = by_id(w.live().snapshot())
    assert (s["a"]["name"], s["a"]["named"]) == ("My name", True)
    assert (s["b"]["name"], s["b"]["named"]) == ("Only auto", False)
    assert s["c"]["name"] == "gamma-17" and s["d"]["name"] == "delta"


def test_rename_mid_session_is_picked_up_incrementally(w):
    w.session(1, "a")
    w.transcript("a", ai("First"))
    live = w.live()
    assert by_id(live.snapshot())["a"]["name"] == "First"
    w.transcript("a", custom("Renamed"))
    assert by_id(live.snapshot())["a"]["name"] == "Renamed"
    w.transcript("a", ai("Later auto"))
    assert by_id(live.snapshot())["a"]["name"] == "Renamed"     # custom keeps winning


def test_title_text_inside_a_message_is_not_mistaken_for_a_title(w):
    w.session(1, "a")
    w.transcript("a", ai("Real"),
                 assistant({"type": "text", "text": 'see {"type":"custom-title","customTitle":"Fake"}'}))
    assert by_id(w.live().snapshot())["a"]["name"] == "Real"


def test_registry_user_name_counts_as_deliberate(w):
    w.session(1, "a", name="Named at launch", name_source="user")
    s = by_id(w.live().snapshot())["a"]
    assert s["name"] == "Named at launch" and s["named"] is True


def test_key_files_are_never_opened(w, monkeypatch):
    w.session(1, "a")
    real_open = open

    def guarded(path, *a, **k):
        assert not str(path).endswith(".key"), "must never open .key files"
        return real_open(path, *a, **k)
    monkeypatch.setattr("builtins.open", guarded)
    assert len(w.live().snapshot()) == 1


def test_hint_about_other_sessions():
    mk = lambda st: {"state": st}
    assert sl.hint([mk("working")]) == ""
    assert sl.hint([mk("working"), mk("needs_you"), mk("working")]) == "+1 needs you"
    assert sl.hint([mk("needs_you"), mk("working"), mk("thinking")]) == "+2 working"
    assert sl.hint([mk("working"), mk("idle"), mk("sleep")]) == "+2 open"


def test_app_bridge_name_hint_and_anim_without_buddy():
    """The gadget gets the top session's name + hint, and an animation even
    when the desktop buddy isn't running."""
    pytest.importorskip("webview")
    import claude_sessions as cs
    import clawdmeter as cm

    class FakeService:
        def latest(self):
            return [{"state": "needs_you", "name": "Fix login bug"},
                    {"state": "working", "name": "Other"}]

    class PartyBuddy:                 # desktop buddy dancing in party mode
        def current_anim(self):
            return "dance bounce"

    api = cs.Api.__new__(cs.Api)
    api.settings = {"clawdmeter_buddy": True}
    api.buddy = PartyBuddy()
    api._sessions_service = FakeService()
    assert api._clawd_session() == {"n": "Fix login bug", "h": "+1 working"}
    assert api._clawd_anim() == "allow"            # the session, not the party
    api._sessions_service = type("S", (), {"latest": lambda self: []})()
    assert api._clawd_anim() == "idle look around"  # no session open
    api._sessions_service = None
    assert api._clawd_anim() == "dance bounce"      # no registry: mirror the buddy
    api.settings["clawdmeter_buddy"] = False
    assert api._clawd_anim() == ""                     # mirroring off: device decides

    link = cm.ClawdmeterLink(session_provider=lambda: {"n": "Café · naïve → «x»? ok", "h": "+2 working"})
    assert link._current_session() == ("Caf nave x? ok", "+2 working", None, "")


def usage(inp=0, read=0, create=0, out=0):
    return {"input_tokens": inp, "cache_read_input_tokens": read,
            "cache_creation_input_tokens": create, "output_tokens": out}


def reply(tokens, model="claude-opus-5-5", requested=None, **extra):
    """Assistant entry with usage; `tokens` split over the three input fields."""
    e = {"type": "assistant", "message": {"role": "assistant", "model": model, "content": [],
                                          "usage": usage(10, tokens - 110, 100, out=999)}}
    if requested:
        e["requestedModel"] = requested
    e.update(extra)
    return e


def test_context_tokens_from_last_main_usage_entry(w):
    w.session(1, "a")
    w.transcript("a", reply(50_000),
                 reply(330_000),
                 reply(900_000, isSidechain=True),                     # subagent
                 reply(950_000, isApiErrorMessage=True),               # error placeholder
                 reply(0, model="<synthetic>"),
                 {"type": "user", "message": {"role": "user", "content": "hi"},
                  "toolUseResult": {"usage": usage(777_777)}})        # nested "usage" key
    s = by_id(w.live().snapshot())["a"]
    # input 10 + cache read + cache creation 100; output tokens don't count
    assert s["ctx_tokens"] == 330_000
    assert s["ctx_window"] == 1_000_000 and s["ctx_pct"] == 33


def test_context_unknown_without_usage_entry(w):
    w.session(1, "a")
    w.transcript("a", ai("Title"))
    w.session(2, "b")                                     # no transcript at all
    s = by_id(w.live().snapshot())
    for sid in ("a", "b"):
        assert s[sid]["ctx_pct"] is None and s[sid]["ctx_window"] is None


def test_context_is_read_incrementally(w):
    w.session(1, "a")
    w.transcript("a", reply(20_000, model="claude-haiku-4-5-20251001"))
    live = w.live()
    s = by_id(live.snapshot())["a"]
    assert (s["ctx_tokens"], s["ctx_window"], s["ctx_pct"]) == (20_000, 200_000, 10)
    # Change the already-read line in place (same length): a reader that
    # starts over would now see 90_000; an incremental one never looks back.
    p = w.root / "projects" / "C--work-proj" / "a.jsonl"
    p.write_bytes(p.read_bytes().replace(b"19890", b"89890"))
    w.transcript("a", ai("Title"))                          # new bytes, no usage
    assert by_id(live.snapshot())["a"]["ctx_tokens"] == 20_000
    w.transcript("a", reply(41_000, model="claude-haiku-4-5-20251001"))
    s = by_id(live.snapshot())["a"]
    assert s["ctx_tokens"] == 41_000 and s["ctx_pct"] == 20


def test_window_rule():
    cw = sl.context_window
    M, K = 1_000_000, 200_000
    # 1. explicit per-session signal
    assert cw("claude-opus-4-6[1m]") == M
    assert cw("claude-opus-4-6", beta_1m=True) == M
    # 2. already past 200K
    assert cw("claude-sonnet-4-5", peak_tokens=200_001) == M
    assert cw("claude-sonnet-4-5", peak_tokens=200_000) == K
    # 3. model families with a native 1M window
    for m in ("claude-opus-5-5", "claude-opus-5", "claude-opus-4-8", "claude-fable-5-1",
              "claude-fable-5", "claude-opus-6-20270101"):
        assert cw(m) == M, m
    for m in ("claude-opus-4-6", "claude-opus-4-20250514", "claude-haiku-4-5-20251001",
              "claude-haiku-5-5", "claude-sonnet-4-5", "something-else"):
        assert cw(m) == K, m
    # 4. settings.json default only when the session's model is unknown
    assert cw("", default_model="opus[1m]") == M
    assert cw("claude-opus-4-6", default_model="opus[1m]") == K
    assert cw("") == K
    assert (sl.window_label(M), sl.window_label(K), sl.window_label(2_000_000)) == ("1M", "200K", "2M")


def test_window_follows_requested_model_and_beta(w):
    w.session(1, "a")
    w.transcript("a", reply(30_000, model="claude-opus-4-6", requested="claude-opus-4-6[1m]"))
    w.session(2, "b")
    w.transcript("b", {"type": "api-request", "querySource": "repl_main_thread",
                       "params": {"model": "claude-opus-4-6",
                                  "betas": ["context-management-2025-06-27", "context-1m-2025-08-07"]}},
                 reply(30_000, model="claude-opus-4-6"), folder="C--b")
    w.session(3, "c")
    w.transcript("c", reply(30_000, model="claude-opus-4-6"), folder="C--c")
    s = by_id(w.live().snapshot())
    assert s["a"]["ctx_window"] == s["b"]["ctx_window"] == 1_000_000
    assert (s["c"]["ctx_window"], s["c"]["ctx_pct"]) == (200_000, 15)


def test_window_peak_resets_on_model_switch(w):
    w.session(1, "a")
    w.transcript("a", reply(300_000, model="claude-sonnet-4-5"))
    live = w.live()
    assert by_id(live.snapshot())["a"]["ctx_window"] == 1_000_000     # past 200K: must be 1M
    w.transcript("a", reply(50_000, model="claude-sonnet-4-5"))       # compacted, same model
    assert by_id(live.snapshot())["a"]["ctx_window"] == 1_000_000
    w.transcript("a", reply(50_000, model="claude-haiku-4-5"))
    assert by_id(live.snapshot())["a"]["ctx_window"] == 200_000


def test_app_bridge_sends_context_and_state():
    pytest.importorskip("webview")
    import claude_sessions as cs

    import time as _time
    waited_since = _time.time() - 60          # open for a minute: counts as waiting

    class FakeService:
        def latest(self):
            return [{"state": "needs_you", "name": "Fix login bug", "ctx_pct": 33,
                     "ctx_window": 1_000_000, "since": waited_since},
                    {"state": "thinking", "name": "Other", "ctx_pct": 7, "ctx_window": 200_000},
                    {"state": "done", "name": "New", "ctx_pct": None, "ctx_window": None},
                    {"state": "sleep", "name": "Old"}]

    api = cs.Api.__new__(cs.Api)
    api.settings = {"clawdmeter_buddy": True}
    api._sessions_service = FakeService()
    api._accounts_mgr = lambda: type("M", (), {"accounts": lambda self: []})()
    assert api._clawd_session() == {"n": "Fix login bug", "h": "+1 working", "cx": 33, "cw": "1M"}
    ss = api._clawd_extras()["se"]
    assert [(x["s"], x.get("c"), x.get("w")) for x in ss] == [
        ("n", 33, "1M"), ("w", 7, "200K"), ("d", None, None), ("z", None, None)]
    assert "c" not in ss[2] and "w" not in ss[2]
    # Wait start in local seconds, like "t" in the usage payload; only on waits.
    assert ss[0]["ws"] == int(waited_since) + _time.localtime(waited_since).tm_gmtoff
    assert all("ws" not in x for x in ss[1:])
    api._sessions_service = type("S", (), {"latest": lambda self: [
        {"state": "working", "name": "Fresh", "ctx_pct": None, "ctx_window": None}]})()
    assert api._clawd_session() == {"n": "Fresh", "h": ""}
    api._sessions_service = type("S", (), {"latest": lambda self: [
        {"state": "working", "name": f"S{i}", "started": i} for i in range(11)]})()
    assert [x["n"] for x in api._clawd_extras()["se"]] == [f"S{i}" for i in range(8)]


def test_gadget_view_debounces_waits_and_orders_stably():
    from sessions_live import gadget_view, WAIT_ARM_S
    now = 10_000.0

    def s(sid, state, since=now - 600, started=0.0, name=None):
        return {"session_id": sid, "state": state, "since": since, "started": started,
                "name": name or sid}

    snap = [s("fresh-q", "needs_you", since=now - 2, started=5),    # just opened
            s("old-q", "needs_you", since=now - 300, started=9),
            s("new-q", "needs_you", since=now - 30, started=1),
            s("idle", "idle", started=2),
            s("busy-b", "working", since=now - 1, started=4),       # newest change
            s("busy-a", "thinking", since=now - 500, started=3),
            s("zz", "sleep", started=0)]
    view = gadget_view(snap, now)
    assert [e["session_id"] for e in view] == [
        "old-q", "new-q",                 # counted waits, longest-waiting first
        "busy-a", "busy-b", "fresh-q",    # working group by start, debounced wait inside
        "idle", "zz"]
    by = {e["session_id"]: e for e in view}
    assert by["fresh-q"]["gstate"] == "working" and by["fresh-q"]["wait_since"] is None
    assert by["old-q"]["gstate"] == "needs_you" and by["old-q"]["wait_since"] == now - 300
    assert by["fresh-q"]["state"] == "needs_you"            # the raw state stays for the Buddy
    # Exactly at the threshold it counts; a status flip doesn't reorder the rest.
    assert gadget_view([s("q", "needs_you", since=now - WAIT_ARM_S)], now)[0]["gstate"] == "needs_you"
    snap[4]["since"] = now                                  # busy-b flips busy <-> shell
    assert [e["session_id"] for e in gadget_view(snap, now)] == [e["session_id"] for e in view]
    # A wait without a known start can't be timed, so it doesn't count.
    assert gadget_view([s("q", "needs_you", since=0)], now)[0]["gstate"] == "working"


def test_gadget_view_limit_and_unique_names():
    from sessions_live import gadget_view
    snap = [{"session_id": str(i), "state": "working", "since": 0, "started": i,
             "name": "clawdmeter" if i < 3 else f"s{i}"} for i in range(10)]
    snap.append({"session_id": "x", "state": "sleep", "since": 0, "started": -1, "name": "zz"})
    view = gadget_view(snap, 100.0, limit=8)
    assert len(view) == 8 and "zz" not in [e["name"] for e in view]     # least urgent dropped
    assert [e["gname"] for e in view[:3]] == ["clawdmeter", "clawdmeter~2", "clawdmeter~3"]
    # The suffix fits inside the length the gadget gets.
    long = [{"session_id": str(i), "state": "working", "since": 0, "started": i,
             "name": "A very long session name"} for i in range(2)]
    names = [e["gname"] for e in gadget_view(long, 0, form=lambda n: n[:20], name_max=20)]
    assert names == ["A very long session", "A very long sessio~2"]
    assert all(len(n) <= 20 for n in names) and len(set(names)) == 2


def test_app_focus_request_resolves_the_unique_gadget_name():
    pytest.importorskip("webview")
    import claude_sessions as cs
    rows = [{"session_id": "a", "state": "working", "since": 0, "started": 1,
             "name": "clawdmeter", "pid": 11},
            {"session_id": "b", "state": "working", "since": 0, "started": 2,
             "name": "clawdmeter", "pid": 22}]
    api = cs.Api.__new__(cs.Api)
    api.settings = {"clawdmeter_buddy": True}
    api._sessions_service = type("S", (), {"latest": lambda self: rows})()
    api._accounts_mgr = lambda: type("M", (), {"accounts": lambda self: []})()
    assert [x["n"] for x in api._clawd_extras()["se"]] == ["clawdmeter", "clawdmeter~2"]
    assert api._clawd_resolve_session(1, "clawdmeter~2")[0]["pid"] == 22
    assert api._clawd_resolve_session(0, "clawdmeter")[0]["pid"] == 11
    # Index stale (list changed): the unique name still finds the right one.
    assert api._clawd_resolve_session(0, "clawdmeter~2")[0]["pid"] == 22
