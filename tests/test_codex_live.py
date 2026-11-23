"""CodexLive: open terminal Codex sessions as live-session dicts (fake homes, injected procs and clock)."""
from __future__ import annotations

import os
from pathlib import Path

import sessions_live as sl
from codex_store import CodexLive
from tests.test_codex_store import T, ev, proc, sessions, token_count, write_rollout

ID1 = "019a0000-0000-7000-8000-000000000001"
ID2 = "019a0000-0000-7000-8000-000000000002"


def live(tmp_path, recs, now=T, mtime=None, title=None, ids=(ID1,), **kw):
    for id_ in ids:
        write_rollout(sessions(tmp_path), id_, recs, mtime=mtime)
    if title:
        (tmp_path / "session_index.jsonl").write_text(
            f'{{"id": "{ids[0]}", "thread_name": "{title}"}}\n', encoding="utf-8")
    procs = [proc(100 + i, T - 500, "resume", id_) for i, id_ in enumerate(ids)]
    return CodexLive(tmp_path, clock=lambda: now, procs=procs, **kw)


def one(c):
    snap = c.snapshot()
    assert len(snap) == 1
    return snap[0]


def test_codex_live_state_working_while_turn_open(tmp_path):
    s = one(live(tmp_path, [ev("task_started", T - 5)]))
    assert s["state"] == sl.WORKING and s["status"] == "busy" and s["tool"] == "codex" and s["pid"] == 100


def test_codex_live_state_limit(tmp_path):
    s = one(live(tmp_path, [token_count(T - 10, 100, resets=T + 3600), ev("task_complete", T - 9)]))
    assert s["state"] == sl.LIMIT


def test_codex_live_expired_limit_is_not_limit(tmp_path):
    s = one(live(tmp_path, [token_count(T - 10, 100, resets=T - 1), ev("task_complete", T - 9)]))
    assert s["state"] == sl.DONE


def test_codex_live_limit_clears_once_the_window_that_hit_has_reset(tmp_path):
    tc = token_count(T - 10, 100, window=300, resets=T - 1, reached="primary",
                     secondary={"used_percent": 40.0, "window_minutes": 10080, "resets_at": T + 500})
    s = one(live(tmp_path, [tc, ev("task_complete", T - 9)]))
    assert s["state"] == sl.DONE


def test_codex_live_limit_while_reached_and_nothing_reset(tmp_path):
    tc = token_count(T - 10, 40, window=300, resets=T + 60, reached="primary")
    assert one(live(tmp_path, [tc, ev("task_complete", T - 9)]))["state"] == sl.LIMIT


def test_codex_live_done_idle_sleep_by_age(tmp_path):
    for age, want in ((sl.DONE_FOR_S - 1, sl.DONE), (sl.DONE_FOR_S + 1, sl.IDLE), (sl.SLEEP_AFTER_S - 1, sl.IDLE),
                      (sl.SLEEP_AFTER_S + 1, sl.SLEEP)):
        home = tmp_path / str(age)
        s = one(live(home, [ev("task_complete", T - age)]))
        assert s["state"] == want and s["status"] == "idle"


def test_codex_live_name_title_else_folder(tmp_path):
    s = one(live(tmp_path / "a", [ev("task_complete", T - 1)], title="Fix the bug"))
    assert s["name"] == "Fix the bug" and s["named"] is True
    s = one(live(tmp_path / "b", [ev("task_complete", T - 1)]))
    assert s["name"] == "x" and s["named"] is False and s["folder"] == "x" and s["cwd"].endswith("x")


def test_codex_live_never_needs_you(tmp_path):
    s = one(live(tmp_path, [ev("task_started", T - 1), ev("exec_approval_request", T - 1)]))
    assert s["state"] != sl.NEEDS_YOU and s["waiting_for"] == ""


def test_codex_live_context_and_keys(tmp_path):
    s = one(live(tmp_path, [token_count(T - 5, 10, ctx=237500), ev("task_complete", T - 4)]))
    assert s["ctx_pct"] == 50 and s["ctx_tokens"] == 237500 and s["ctx_window"] == 475000
    assert set(s) >= {"session_id", "pid", "cwd", "folder", "name", "named", "status", "waiting_for", "state",
                      "since", "started", "ctx_tokens", "ctx_window", "ctx_pct", "tool"}


def test_codex_live_only_running_sessions(tmp_path):
    c = live(tmp_path, [ev("task_complete", T - 1)], ids=(ID1, ID2))
    c.procs = c.procs[:1]
    assert [s["session_id"] for s in c.snapshot()] == [ID1]


def test_codex_live_rereads_tail_only_when_file_changes(tmp_path, monkeypatch):
    import codex_store
    path = write_rollout(sessions(tmp_path), ID1, [ev("task_started", T - 5)])
    c = CodexLive(tmp_path, clock=lambda: T, procs=[proc(1, T - 500, "resume", ID1)])
    calls = []
    real = codex_store.read_tail
    monkeypatch.setattr(codex_store, "read_tail", lambda p: calls.append(p) or real(p))
    assert c.snapshot()[0]["state"] == sl.WORKING
    c.snapshot()
    assert len(calls) == 1
    write_rollout(sessions(tmp_path), ID1, [ev("task_started", T - 5), ev("task_complete", T - 1)])
    os.utime(path, (T + 7, T + 7))
    assert c.snapshot()[0]["state"] == sl.DONE and len(calls) == 2


def test_codex_live_enumeration_is_throttled(tmp_path, monkeypatch):
    import codex_store
    write_rollout(sessions(tmp_path), ID1, [ev("task_complete", T - 1)])
    now = [T]
    n = []
    real = codex_store.live_threads
    monkeypatch.setattr(codex_store, "live_threads", lambda h, p=None: n.append(1) or real(h, p))
    c = CodexLive(tmp_path, clock=lambda: now[0], procs=[proc(1, T - 500, "resume", ID1)])
    c.snapshot(); now[0] += 1; c.snapshot()
    assert len(n) == 1
    now[0] += 5; c.snapshot()
    assert len(n) == 2


def test_codex_live_folder_from_windows_cwd(tmp_path):
    import json
    p = write_rollout(sessions(tmp_path), ID1, [ev("task_complete", T - 1)])
    lines = p.read_text(encoding="utf-8").splitlines()
    meta = json.loads(lines[0])
    meta["payload"]["cwd"] = "C:" + chr(92) + "proj" + chr(92) + "my app" + chr(92)
    p.write_text(chr(10).join([json.dumps(meta)] + lines[1:]) + chr(10), encoding="utf-8")
    s = one(CodexLive(tmp_path, clock=lambda: T, procs=[proc(1, T - 500, "resume", ID1)]))
    assert s["folder"] == "my app"
