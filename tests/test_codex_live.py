"""CodexLive: open terminal Codex sessions as live-session dicts (fake homes, injected procs and clock)."""
from __future__ import annotations

import json
from pathlib import Path

import sessions_live as sl
from codex_store import CodexLive
from tests.test_codex_store import T, ev, iso, proc, sessions, token_count, write_rollout

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


def append(path: Path, *recs: dict, raw: bytes = b"") -> None:
    """Append records (as `ev` makes them, with "ts") the way Codex does, plus optional raw bytes."""
    with open(path, "ab") as fh:
        for r in recs:
            fh.write(json.dumps({"timestamp": iso(r.get("ts", T)), "type": r.get("type", "event_msg"),
                                 "payload": r["payload"]}).encode() + b"\n")
        fh.write(raw)


def counting_seed(monkeypatch):
    import codex_store
    calls = []
    real = codex_store._seed
    monkeypatch.setattr(codex_store, "_seed", lambda p, *a: calls.append(p) or real(p, *a))
    return calls


def test_codex_live_reads_only_what_was_appended(tmp_path, monkeypatch):
    path = write_rollout(sessions(tmp_path), ID1, [ev("task_started", T - 5)])
    c = CodexLive(tmp_path, clock=lambda: T, procs=[proc(1, T - 500, "resume", ID1)])
    seeds = counting_seed(monkeypatch)
    assert c.snapshot()[0]["state"] == sl.WORKING
    c.snapshot()
    assert len(seeds) == 1
    append(path, ev("task_complete", T - 1))
    assert c.snapshot()[0]["state"] == sl.DONE and len(seeds) == 1   # growth is read incrementally


def test_codex_live_long_turn_stays_open_past_the_tail_cap(tmp_path, monkeypatch):
    """A turn whose task_started lies further back than the bounded tail read stays open while lines without turn
    events append, and closes on task_complete."""
    import codex_store
    path = write_rollout(sessions(tmp_path), ID1, [ev("task_started", T - 50)])
    c = CodexLive(tmp_path, clock=lambda: T, procs=[proc(1, T - 500, "resume", ID1)])
    seeds = counting_seed(monkeypatch)
    assert c.snapshot()[0]["state"] == sl.WORKING
    monkeypatch.setattr(codex_store, "TAIL_MAX_BYTES", 4096)
    for i in range(40):
        append(path, ev("agent_message", T - 40 + i, message="x" * 400))
    s = c.snapshot()[0]
    assert s["state"] == sl.WORKING and s["since"] == T - 1
    append(path, token_count(T - 1, 10.0, ctx=237500), ev("task_complete", T - 1))
    s = c.snapshot()[0]
    assert s["state"] == sl.DONE and s["ctx_pct"] == 50 and len(seeds) == 1


def test_codex_live_partial_line_is_read_once_complete(tmp_path, monkeypatch):
    path = write_rollout(sessions(tmp_path), ID1, [ev("task_started", T - 5)])
    c = CodexLive(tmp_path, clock=lambda: T, procs=[proc(1, T - 500, "resume", ID1)])
    line = json.dumps({"timestamp": iso(T - 1), "type": "event_msg", "payload": {"type": "task_complete"}}).encode()
    append(path, raw=line[:30])
    assert c.snapshot()[0]["state"] == sl.WORKING
    append(path, raw=line[30:] + b"\n")
    assert c.snapshot()[0]["state"] == sl.DONE


def test_codex_live_shrunk_file_reseeds(tmp_path, monkeypatch):
    path = write_rollout(sessions(tmp_path), ID1, [ev("task_started", T - 5)] +
                         [ev("agent_message", T - 4, message="x" * 400) for _ in range(5)])
    c = CodexLive(tmp_path, clock=lambda: T, procs=[proc(1, T - 500, "resume", ID1)])
    seeds = counting_seed(monkeypatch)
    assert c.snapshot()[0]["state"] == sl.WORKING
    write_rollout(sessions(tmp_path), ID1, [ev("task_complete", T - 2)])   # rewritten, smaller
    assert path.stat().st_size < 1000
    assert c.snapshot()[0]["state"] == sl.DONE and len(seeds) == 2


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
