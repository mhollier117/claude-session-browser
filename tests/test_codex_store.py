"""codex_store: reads Codex's own files (a fake home per test, never the real ~/.codex)."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import codex_store
from codex_store import (
    CodexTail, codex_home, imported_ids, is_terminal, live_threads, read_meta, read_tail,
    terminal_rollouts, titles, usage,
)

T = 1_795_000_000.0  # a fixed "now"


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def write_rollout(dir_: Path, id_: str, records: list[dict], source="cli", mtime: float | None = None,
                  start: float = T - 1000) -> Path:
    """A rollout under dir_/YYYY/MM/DD; `records` are {"type", "payload", optional "ts"} dicts."""
    day = dir_ / "2026" / "10" / "26"
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"rollout-2026-10-26T12-35-30-{id_}.jsonl"
    meta = {"timestamp": iso(start), "type": "session_meta",
            "payload": {"id": id_, "cwd": "C:\\x", "originator": "codex-tui", "source": source}}
    lines = [json.dumps(meta)]
    for i, r in enumerate(records):
        lines.append(json.dumps({"timestamp": iso(r.get("ts", start + 1 + i)), "type": r.get("type", "event_msg"),
                                 "payload": r["payload"]}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def ev(kind: str, ts: float | None = None, **extra) -> dict:
    rec = {"payload": {"type": kind, **extra}}
    if ts is not None:
        rec["ts"] = ts
    return rec


def token_count(ts: float, used: float | None, window=10080, resets=T + 3600, reached=None, ctx=141445,
                plan="prolite", secondary=None) -> dict:
    rl = {"limit_id": "codex", "primary": {"used_percent": used, "window_minutes": window, "resets_at": resets},
          "secondary": secondary, "credits": {}, "plan_type": plan, "rate_limit_reached_type": reached}
    return ev("token_count", ts, info={"last_token_usage": {"input_tokens": ctx}, "total_token_usage": {},
                                       "model_context_window": 475000}, rate_limits=rl)


def sessions(home: Path) -> Path:
    return home / "sessions"


# ---------------------------------------------------------------- home / meta / terminal

def test_codex_home_env_and_default(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "h"))
    assert codex_home() == tmp_path / "h"
    monkeypatch.delenv("CODEX_HOME")
    assert codex_home() == Path.home() / ".codex"


def test_read_meta_first_line_only(tmp_path):
    p = write_rollout(sessions(tmp_path), "a1", [])
    assert read_meta(p)["id"] == "a1"
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"type": "event_msg", "payload": {}}) + "\n")
    assert read_meta(bad) is None
    assert read_meta(tmp_path / "missing.jsonl") is None


def test_is_terminal_filters_subagents_vscode_and_imports(tmp_path):
    assert is_terminal({"id": "a", "source": "cli"}, set()) is True
    assert is_terminal({"id": "a", "source": {"subagent": {"x": 1}}}, set()) is False
    assert is_terminal({"id": "a", "source": "vscode"}, set()) is False
    assert is_terminal({"id": "a", "source": "cli"}, {"a"}) is False


def test_imported_ids(tmp_path):
    assert imported_ids(tmp_path) == set()
    (tmp_path / "external_agent_session_imports.json").write_text(
        json.dumps({"records": [{"imported_thread_id": "x"}, {"other": 1}], "detected_connector_records": []}))
    assert imported_ids(tmp_path) == {"x"}
    (tmp_path / "external_agent_session_imports.json").write_text("garbage")
    assert imported_ids(tmp_path) == {"x"}   # a failed read (mid-rewrite) keeps the last good set


def test_imported_ids_last_good_set_per_home_and_a_copy(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    (a / "external_agent_session_imports.json").write_text(json.dumps({"records": [{"imported_thread_id": "x"}]}))
    got = imported_ids(a)
    got.add("mutated")
    assert imported_ids(a) == {"x"}
    (a / "external_agent_session_imports.json").write_text('{"records": [')       # truncated mid-rewrite
    assert imported_ids(a) == {"x"}
    (a / "external_agent_session_imports.json").write_text(json.dumps({"other": 1}))  # no records list
    assert imported_ids(a) == {"x"}
    (b / "external_agent_session_imports.json").write_text("garbage")
    assert imported_ids(b) == set()                                                  # never a good read here
    (a / "external_agent_session_imports.json").write_text(json.dumps({"records": []}))
    assert imported_ids(a) == set()                                                  # a good, empty read


def test_terminal_rollouts_newest_file_wins(tmp_path):
    s = sessions(tmp_path)
    old = write_rollout(s, "dup", [], mtime=T - 500)
    new = tmp_path / "sessions" / "2026" / "11" / "01" / "rollout-2026-11-01T00-00-00-dup.jsonl"
    new.parent.mkdir(parents=True)
    new.write_bytes(old.read_bytes())
    os.utime(new, (T, T))
    write_rollout(s, "sub", [], source={"subagent": {"a": 1}})
    write_rollout(s, "imp", [])
    (tmp_path / "external_agent_session_imports.json").write_text(
        json.dumps({"records": [{"imported_thread_id": "imp"}]}))
    assert terminal_rollouts(tmp_path) == {"dup": new}


def counting_opens(monkeypatch) -> list:
    """Every `open` codex_store makes (read_meta, the rollout heads), recorded by path."""
    import builtins
    opened = []
    monkeypatch.setattr(codex_store, "open", lambda p, *a, **k: opened.append(Path(p)) or builtins.open(p, *a, **k),
                        raising=False)
    return opened


def test_terminal_rollouts_reads_each_first_line_once(tmp_path, monkeypatch):
    s = sessions(tmp_path)
    a = write_rollout(s, "a", [])
    write_rollout(s, "b", [])
    opened = counting_opens(monkeypatch)
    assert set(terminal_rollouts(tmp_path)) == {"a", "b"} and len(opened) == 2
    opened.clear()
    assert set(terminal_rollouts(tmp_path)) == {"a", "b"} and opened == []       # unchanged files: no reads
    c = tmp_path / "sessions" / "2026" / "11" / "02" / "rollout-2026-11-02T00-00-00-c.jsonl"
    c.parent.mkdir(parents=True)
    c.write_bytes(a.read_bytes().replace(b'"id": "a"', b'"id": "c"'))
    assert set(terminal_rollouts(tmp_path)) == {"a", "b", "c"} and opened == [c]  # only the new one
    a.unlink()
    assert set(terminal_rollouts(tmp_path)) == {"b", "c"}
    assert a not in codex_store._ROLLOUT_CACHE[tmp_path]                         # vanished paths dropped


def test_terminal_rollouts_applies_current_imports_to_cached_metas(tmp_path, monkeypatch):
    write_rollout(sessions(tmp_path), "a", [])
    assert set(terminal_rollouts(tmp_path)) == {"a"}
    opened = counting_opens(monkeypatch)
    (tmp_path / "external_agent_session_imports.json").write_text(
        json.dumps({"records": [{"imported_thread_id": "a"}]}))
    assert terminal_rollouts(tmp_path) == {} and opened == []


def test_terminal_rollouts_never_caches_a_half_written_first_line(tmp_path):
    day = sessions(tmp_path) / "2026" / "10" / "26"
    day.mkdir(parents=True)
    p = day / "rollout-2026-10-26T12-35-30-a.jsonl"
    full = json.dumps({"timestamp": iso(T), "type": "session_meta", "payload": {"id": "a", "source": "cli"}})
    p.write_text(full[:20], encoding="utf-8")                                     # Codex still writing line 1
    assert terminal_rollouts(tmp_path) == {}
    p.write_text(full + "\n", encoding="utf-8")
    assert terminal_rollouts(tmp_path) == {"a": p}


def test_terminal_rollout_caches_are_per_home(tmp_path):
    write_rollout(sessions(tmp_path / "h1"), "a", [])
    write_rollout(sessions(tmp_path / "h2"), "b", [])
    assert set(terminal_rollouts(tmp_path / "h1")) == {"a"}
    assert set(terminal_rollouts(tmp_path / "h2")) == {"b"}
    assert set(terminal_rollouts(tmp_path / "h1")) == {"a"}


# ---------------------------------------------------------------- titles

def make_sqlite(home: Path, rows: list[tuple[str, str]]):
    con = sqlite3.connect(home / "state_5.sqlite")
    con.execute("create table threads (id text, title text, source text, cwd text, rollout_path text)")
    con.executemany("insert into threads (id, title) values (?, ?)", rows)
    con.commit()
    con.close()


def test_titles_session_index_overrides_sqlite(tmp_path):
    make_sqlite(tmp_path, [("t1", "From sqlite"), ("t2", "Only sqlite"), ("t3", "")])
    (tmp_path / "session_index.jsonl").write_text(
        "\n".join(json.dumps(r) for r in [
            {"id": "t1", "thread_name": "First", "updated_at": "x"},
            {"id": "t1", "thread_name": "Last wins", "updated_at": "y"},
            {"id": "t4", "thread_name": "Index only"}]) + "\nnot json\n")
    assert titles(tmp_path) == {"t1": "Last wins", "t2": "Only sqlite", "t4": "Index only"}


def test_titles_survive_missing_or_corrupt_sqlite(tmp_path):
    (tmp_path / "session_index.jsonl").write_text(json.dumps({"id": "t1", "thread_name": "From index"}) + "\n")
    assert titles(tmp_path) == {"t1": "From index"}  # no sqlite at all
    (tmp_path / "state_5.sqlite").write_bytes(b"not a database")
    assert titles(tmp_path) == {"t1": "From index"}


def test_titles_never_creates_sqlite(tmp_path):
    titles(tmp_path)
    assert not (tmp_path / "state_5.sqlite").exists()


# ---------------------------------------------------------------- read_tail

def test_read_tail_turn_open_until_task_complete(tmp_path):
    p = write_rollout(sessions(tmp_path), "a", [ev("task_started"), ev("agent_message")])
    t = read_tail(p)
    assert isinstance(t, CodexTail) and t.turn_open is True
    p = write_rollout(sessions(tmp_path), "a", [ev("task_started"), ev("task_complete")])
    assert read_tail(p).turn_open is False
    p = write_rollout(sessions(tmp_path), "a", [ev("task_started"), ev("turn_aborted")])
    assert read_tail(p).turn_open is False
    p = write_rollout(sessions(tmp_path), "a", [ev("task_started"), ev("task_complete"), ev("task_started")])
    assert read_tail(p).turn_open is True
    assert read_tail(p).last_ts == T - 1000 + 3


def test_read_tail_rate_limits_and_context(tmp_path):
    p = write_rollout(sessions(tmp_path), "a", [
        ev("thread_settings_applied", thread_settings={"model": "gpt-6.1-sol"}),
        token_count(T - 10, 12.0, resets=T + 3600), ev("agent_message")])
    t = read_tail(p)
    assert t.ctx_tokens == 141445 and t.ctx_window == 475000
    assert t.rate_limits["primary"]["used_percent"] == 12.0
    assert t.rate_ts == T - 10
    assert t.limit_hit is False
    assert t.model == "gpt-6.1-sol"


def test_read_tail_model_from_newest_turn_context(tmp_path):
    p = write_rollout(sessions(tmp_path), "a", [
        ev("thread_settings_applied", thread_settings={"model": "from-settings"}),
        {"type": "turn_context", "payload": {"model": "gpt-old"}},
        {"type": "turn_context", "payload": {"model": "gpt-6.1-sol"}}, ev("task_started")])
    assert read_tail(p).model == "gpt-6.1-sol"
    p = write_rollout(sessions(tmp_path), "a", [ev("thread_settings_applied", thread_settings={"model": "m2"})])
    assert read_tail(p).model == "m2"


def test_read_tail_null_info_and_no_events(tmp_path):
    p = write_rollout(sessions(tmp_path), "a", [ev("token_count", info=None, rate_limits=None)])
    t = read_tail(p)
    assert (t.ctx_tokens, t.ctx_window, t.rate_limits, t.rate_ts, t.model) == (None, None, None, None, None)
    assert t.turn_open is False and t.limit_hit is False
    assert read_tail(tmp_path / "missing.jsonl") == CodexTail(None, False, False, None, None, None, None, None)


def test_read_tail_limit_hit_when_rate_limit_reached_type_set(tmp_path):
    p = write_rollout(sessions(tmp_path), "a", [token_count(T, 40.0, reached="primary")])
    assert read_tail(p).limit_hit is True
    p = write_rollout(sessions(tmp_path), "a", [token_count(T, 100.0)])
    assert read_tail(p).limit_hit is True
    # the latest record decides: a later, clear one resets it
    p = write_rollout(sessions(tmp_path), "a", [token_count(T, 100.0, reached="primary"), token_count(T + 1, 5.0)])
    assert read_tail(p).limit_hit is False


def test_read_tail_skips_partial_last_line(tmp_path):
    p = write_rollout(sessions(tmp_path), "a", [ev("task_started")])
    with open(p, "ab") as fh:
        fh.write(b'{"timestamp": "2026-10-26T12:35:30.298Z", "type": "event_msg", "payload": {"type": "task_comp')
    assert read_tail(p).turn_open is True


def test_read_tail_small_window_grows_until_found(tmp_path):
    recs = [token_count(T - 100, 33.0), ev("task_started")] + [ev("agent_message", text="x" * 400) for _ in range(60)]
    p = write_rollout(sessions(tmp_path), "a", recs)
    t = read_tail(p, tail_bytes=1024)
    assert t.rate_limits["primary"]["used_percent"] == 33.0 and t.turn_open is True


# ---------------------------------------------------------------- usage

def usage_home(tmp_path, records, **kw):
    write_rollout(sessions(tmp_path), "a", records, **kw)
    return tmp_path


def test_usage_maps_windows_by_length(tmp_path):
    R = T + 3600
    home = usage_home(tmp_path, [token_count(T - 5, 12.0, window=10080, resets=R)])
    u = usage(home, T)
    assert u == {"s": None, "s_reset_at": 0.0, "w": 12, "w_reset_at": R, "limited": False, "plan": "prolite",
                 "at": T - 5}


def test_usage_window_300_is_five_hour(tmp_path):
    home = usage_home(tmp_path, [token_count(T - 5, 30.0, window=300, resets=T + 60,
                                             secondary={"used_percent": 7.0, "window_minutes": 10080,
                                                        "resets_at": T + 9999})])
    u = usage(home, T)
    assert (u["s"], u["s_reset_at"], u["w"], u["w_reset_at"]) == (30, T + 60, 7, T + 9999)


def test_usage_expired_window_reads_zero(tmp_path):
    home = usage_home(tmp_path, [token_count(T - 5, 80.0, window=10080, resets=T - 1)])
    u = usage(home, T)
    assert u["w"] == 0 and u["w_reset_at"] == 0.0


def test_usage_takes_newest_record_by_timestamp_across_rollouts(tmp_path):
    s = sessions(tmp_path)
    write_rollout(s, "old", [token_count(T - 500, 10.0)], mtime=T)          # newest file, older record
    write_rollout(s, "new", [token_count(T - 50, 55.0)], mtime=T - 100)
    assert usage(tmp_path, T)["w"] == 55


def test_usage_limited_follows_newest_record(tmp_path):
    home = usage_home(tmp_path, [token_count(T - 5, 100.0, reached="primary")])
    assert usage(home, T)["limited"] is True


def test_usage_limited_false_when_all_windows_expired(tmp_path):
    home = usage_home(tmp_path, [token_count(T - 5, 100.0, resets=T - 1, reached="primary")])
    u = usage(home, T)
    assert u["w"] == 0 and u["w_reset_at"] == 0.0 and u["limited"] is False


def test_usage_limited_true_when_a_live_window_is_full(tmp_path):
    home = usage_home(tmp_path, [token_count(T - 5, 40.0, window=300, resets=T - 1,
                                             secondary={"used_percent": 100.0, "window_minutes": 10080,
                                                        "resets_at": T + 500})])
    u = usage(home, T)
    assert (u["s"], u["w"], u["limited"]) == (0, 100, True)


def test_usage_limited_false_when_only_the_expired_window_was_full(tmp_path):
    home = usage_home(tmp_path, [token_count(T - 5, 100.0, window=300, resets=T - 1,
                                             secondary={"used_percent": 30.0, "window_minutes": 10080,
                                                        "resets_at": T + 500})])
    u = usage(home, T)
    assert (u["s"], u["w"], u["limited"]) == (0, 30, False)


def test_usage_reached_ignored_once_the_window_that_hit_has_reset(tmp_path):
    """The 5h window hit 100 % and has reset since; the weekly one is live at 40 %: no longer limited."""
    home = usage_home(tmp_path, [token_count(T - 5, 100.0, window=300, resets=T - 1, reached="primary",
                                             secondary={"used_percent": 40.0, "window_minutes": 10080,
                                                        "resets_at": T + 500})])
    u = usage(home, T)
    assert (u["s"], u["w"], u["limited"]) == (0, 40, False)


def test_rate_limited_rules():
    def rl(p=None, s=None, reached=None):
        return {"primary": p, "secondary": s, "rate_limit_reached_type": reached}

    def win(used, resets):
        return {"used_percent": used, "window_minutes": 300, "resets_at": resets}
    lim = codex_store.rate_limited
    assert lim(rl(win(100, T + 9)), T) is True                         # a live full window
    assert lim(rl(win(100, T)), T) is False                            # resets_at <= now: reset
    assert lim(rl(win(40, T + 9), reached="primary"), T) is True       # reached, nothing reset
    assert lim(rl(win(100, T - 1), win(40, T + 9), "primary"), T) is False
    assert lim(rl(win(40, T - 1), win(100, T + 9), "secondary"), T) is True   # the live one is full
    assert lim(rl({"used_percent": 100}), T) is True                   # no resets_at: live
    assert lim(rl(reached="primary"), T) is True                       # reached, no windows to have reset
    assert lim(rl(win(99, T + 9)), T) is False
    assert lim({}, T) is False


def test_usage_none_without_token_count(tmp_path):
    assert usage(tmp_path, T) is None
    home = usage_home(tmp_path / "h2", [ev("task_started")])
    assert usage(home, T) is None


def test_usage_ignores_subagent_rollouts(tmp_path):
    write_rollout(sessions(tmp_path), "sub", [token_count(T - 5, 90.0)], source={"subagent": {}})
    assert usage(tmp_path, T) is None


# ---------------------------------------------------------------- live_threads

ID1 = "01a1ef10-2463-7ec2-828a-163001599fdd"
ID2 = "02b2ef10-2463-7ec2-828a-163001599fdd"
ID3 = "03c3ef10-2463-7ec2-828a-163001599fdd"


def lock(home: Path, id_: str, mtime: float):
    d = home / "thread-writer-locks"
    d.mkdir(exist_ok=True)
    p = d / f"{id_}.lock"
    p.write_bytes(b"")
    os.utime(p, (mtime, mtime))


def proc(pid, ct, *args):
    return {"pid": pid, "create_time": ct, "cmdline": ["codex.exe", *args]}


def test_live_threads_id_from_cmdline(tmp_path):
    write_rollout(sessions(tmp_path), ID1, [])
    procs = [proc(10032, T, "--yolo", "resume", ID1)]
    assert live_threads(tmp_path, procs) == {ID1: 10032}


def test_live_threads_cmdline_id_must_be_terminal(tmp_path):
    procs = [proc(1, T, "resume", ID1)]
    assert live_threads(tmp_path, procs) == {}


def test_live_threads_lock_mtime_matches_picker_process(tmp_path):
    for i in (ID1, ID2):
        write_rollout(sessions(tmp_path), i, [])
    lock(tmp_path, ID1, T + 6)
    lock(tmp_path, ID2, T + 5 + 3)
    (tmp_path / "thread-writer-locks" / ".coordination.lock").write_bytes(b"")
    procs = [proc(100, T, "--yolo", "resume"), proc(200, T + 5, "--yolo", "resume")]
    got = live_threads(tmp_path, procs)
    assert got == {ID1: 200, ID2: 100}
    assert len(set(got)) == 2 and len(set(got.values())) == 2


def test_live_threads_lock_before_the_process_ignored(tmp_path):
    write_rollout(sessions(tmp_path), ID1, [])
    lock(tmp_path, ID1, T - 6)
    assert live_threads(tmp_path, [proc(1, T, "resume")]) == {}


def test_live_threads_lock_made_at_a_late_first_prompt_matches(tmp_path):
    """Codex makes the rollout (and lock) at the first prompt: a process started 10 minutes earlier still gets it."""
    write_rollout(sessions(tmp_path), ID1, [])
    lock(tmp_path, ID1, T + 61)
    assert live_threads(tmp_path, [proc(1, T)]) == {ID1: 1}
    lock(tmp_path, ID1, T + 600)
    assert live_threads(tmp_path, [proc(1, T)]) == {ID1: 1}


def test_live_threads_late_lock_goes_to_one_process_only(tmp_path):
    write_rollout(sessions(tmp_path), ID1, [])
    lock(tmp_path, ID1, T + 600)
    assert live_threads(tmp_path, [proc(1, T), proc(2, T + 10)]) == {ID1: 2}   # the smaller gap
    write_rollout(sessions(tmp_path), ID2, [])
    lock(tmp_path, ID2, T + 700)
    got = live_threads(tmp_path, [proc(1, T), proc(2, T + 10)])
    assert got == {ID1: 2, ID2: 1} and len(set(got.values())) == 2


def test_live_threads_window_match_wins_over_the_late_fallback(tmp_path):
    for i in (ID1, ID2):
        write_rollout(sessions(tmp_path), i, [])
    lock(tmp_path, ID1, T + 2)          # inside proc 1's 60 s window
    lock(tmp_path, ID2, T + 900)        # only reachable by the fallback
    procs = [proc(1, T), proc(2, T - 3000)]
    # one greedy pass over all pairs would give ID2 to proc 1 first (gap 900 is the smallest); the window pass runs
    # first, so proc 1 keeps ID1 and only proc 2 falls back
    assert live_threads(tmp_path, procs) == {ID1: 1, ID2: 2}


def test_live_threads_lock_for_non_terminal_ignored(tmp_path):
    write_rollout(sessions(tmp_path), ID1, [], source="vscode")
    lock(tmp_path, ID1, T + 2)
    assert live_threads(tmp_path, [proc(1, T)]) == {}


def test_live_threads_ignores_app_server_processes(tmp_path):
    write_rollout(sessions(tmp_path), ID1, [])
    lock(tmp_path, ID1, T + 2)
    procs = [proc(1, T, "app-server", "--listen", "stdio")]
    assert live_threads(tmp_path, procs) == {}


def test_live_threads_ignores_lock_without_process(tmp_path):
    write_rollout(sessions(tmp_path), ID1, [])
    lock(tmp_path, ID1, T + 2)
    assert live_threads(tmp_path, []) == {}


def test_live_threads_cmdline_id_claims_before_lock_matching(tmp_path):
    for i in (ID1, ID2):
        write_rollout(sessions(tmp_path), i, [])
    lock(tmp_path, ID1, T + 2)   # belongs to the process that names ID1 on its command line
    lock(tmp_path, ID2, T + 4)
    procs = [proc(1, T, "resume", ID1), proc(2, T + 1, "resume")]
    assert live_threads(tmp_path, procs) == {ID1: 1, ID2: 2}


def test_live_threads_default_procs_without_codex_returns_dict(tmp_path):
    assert isinstance(live_threads(tmp_path), dict)
