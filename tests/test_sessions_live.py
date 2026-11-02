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
    assert link._current_session() == ("Caf nave x? ok", "+2 working")
