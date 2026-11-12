import json
import os
import pathlib
import re

import pytest

import launcher.index as index_mod
import launcher.stores as stores
from launcher.index import FolderStats, Index, Row
from launcher_helpers import write_claude_session, write_qwen_session

T0 = 1_700_000_000.0


def _u(text, cwd=None, ts=None):
    """A user record. Without `ts` it carries no timestamp, so a row's last activity is the file's mtime."""
    rec = {"type": "user", "message": {"role": "user", "content": text}}
    if ts is not None:
        rec["timestamp"] = ts
    if cwd is not None:
        rec["cwd"] = str(cwd)
    return rec


def _a(text):
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def _q_user(text):
    return {"type": "user", "provenance": "real_user", "message": {"role": "user", "parts": [{"text": text}]}}


def _q_asst(text):
    return {"type": "assistant", "message": {"role": "model", "parts": [{"text": text}]}}


def _touch(path, mtime):
    os.utime(path, (mtime, mtime))


def _append(path, *records):
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write("".join(json.dumps(r) + "\n" for r in records))


def _register(env, pid, sid, cwd, name=None):
    rec = {"pid": pid, "sessionId": sid, "cwd": str(cwd), "kind": "interactive"}
    if name:
        rec["name"] = name
    (env.claude_home / "sessions" / f"{pid}.json").write_text(json.dumps(rec), encoding="utf-8")


def _index(env, claude_live=None, qwen_alive=lambda pid: False):
    return Index(env, claude_live=claude_live or (lambda: []), pid_exists=qwen_alive)


def test_refresh_indexes_both_stores_and_sorts_by_activity(fake_env):
    net, ai = fake_env.root / "work" / "net", fake_env.root / "home" / "ai"
    c = write_claude_session(fake_env, net, "c1", [_u("set up PXE", net), _a("Add the VLAN.")])
    q = write_qwen_session(fake_env, ai, "q1", [_q_user("hello qwen"), _q_asst("hi")])
    _touch(c, T0 - 3600)
    _touch(q, T0 - 60)
    idx = _index(fake_env)
    assert idx.refresh() == 2
    rows = idx.rows()
    assert [(r.store, r.sid) for r in rows] == [("qwen", "q1"), ("claude", "c1")]
    r = rows[1]
    assert isinstance(r, Row) and r.cwd == str(net) and r.first_ask == "set up PXE" and r.last_reply == "Add the VLAN."
    assert (r.user_msgs, r.assistant_msgs, r.last_activity, r.live_pid, r.wrapper) == (1, 1, T0 - 3600, None, None)
    assert rows[0].cwd == str(ai) and rows[0].cwd_exists
    assert (fake_env.data_dir / "index.sqlite").is_file()


def test_refresh_rereads_only_changed_files(fake_env, monkeypatch):
    cwd = fake_env.root / "cc"
    p1 = write_claude_session(fake_env, cwd, "c1", [_u("one", cwd)])
    write_claude_session(fake_env, cwd, "c2", [_u("two", cwd)])
    calls = []
    real = stores.read_claude
    monkeypatch.setattr(stores, "read_claude", lambda p, **kw: calls.append(p.stem) or real(p, **kw))
    idx = _index(fake_env)
    assert idx.refresh() == 2 and sorted(calls) == ["c1", "c2"]
    calls.clear()
    assert idx.refresh() == 0 and calls == []
    _append(p1, _a("reply"))
    assert idx.refresh() == 1 and calls == ["c1"]
    assert idx.row("claude", "c1").last_reply == "reply"


def test_counts_are_incremental_when_a_file_grows(fake_env, monkeypatch):
    cwd = fake_env.root / "cc"
    p = write_claude_session(fake_env, cwd, "c1", [_u("one", cwd), _a("r1"), {"type": "ai-title", "aiTitle": "A1"}])
    starts = []
    real = stores.count_messages
    monkeypatch.setattr(stores, "count_messages", lambda path, store, start=0: starts.append(start) or real(path, store, start))
    idx = _index(fake_env)
    idx.refresh()
    first_end = p.stat().st_size
    _append(p, _u("two"), _a("r2"))
    idx.refresh()
    assert starts == [0, first_end]
    r = idx.row("claude", "c1")
    assert (r.user_msgs, r.assistant_msgs, r.display_name) == (2, 2, "A1")  # old ai-title kept
    _append(p, {"type": "custom-title", "customTitle": "Mine"})
    idx.refresh()
    assert idx.row("claude", "c1").display_name == "Mine"


def test_last_ask_beyond_the_tail_cap_keeps_the_stored_one(fake_env):
    cwd = fake_env.root / "cc"
    p = write_claude_session(fake_env, cwd, "c1", [_u("first", cwd), _a("r1"), _u("the last typed ask"), _a("r2")])
    idx = _index(fake_env)
    idx.refresh()
    assert idx.row("claude", "c1").last_ask == "the last typed ask"
    mb = 1024 * 1024
    _append(p, *[_a(f"{i} " + "z" * (mb - 100)) for i in range(18)], _a("final reply"))  # 18 MB of tool run
    assert idx.refresh() == 1
    r = idx.row("claude", "c1")
    assert r.last_reply == "final reply" and r.last_ask == "the last typed ask" and r.assistant_msgs == 21
    _append(p, _u("a new ask"), _a("ok"))
    idx.refresh()
    assert idx.row("claude", "c1").last_ask == "a new ask"  # a newer ask within reach still replaces it
    idx.close()


def test_shrunk_file_is_counted_from_the_start(fake_env, monkeypatch):
    cwd = fake_env.root / "cc"
    p = write_claude_session(fake_env, cwd, "c1", [_u("one", cwd), _a("r1"), _u("two"), _a("r2")])
    idx = _index(fake_env)
    idx.refresh()
    write_claude_session(fake_env, cwd, "c1", [_u("new", cwd)])
    _touch(p, T0)
    idx.refresh()
    r = idx.row("claude", "c1")
    assert (r.user_msgs, r.assistant_msgs, r.first_ask) == (1, 0, "new")


def test_display_name_priority(fake_env):
    cwd = fake_env.root / "cc"
    write_claude_session(fake_env, cwd, "t1", [_u("ask one", cwd), {"type": "custom-title", "customTitle": "Custom"}])
    write_claude_session(fake_env, cwd, "t2", [_u("ask two", cwd), {"type": "custom-title", "customTitle": "Custom"},
                                               {"type": "ai-title", "aiTitle": "AI"}])
    write_claude_session(fake_env, cwd, "t3", [_u("ask three", cwd), {"type": "ai-title", "aiTitle": "AI three"}])
    write_claude_session(fake_env, cwd, "t4", [_u("x" * 100, cwd)])
    write_claude_session(fake_env, cwd, "t5", [_a("no ask")])
    (fake_env.claude_home / "session_titles.json").write_text(json.dumps({"t1": "From titles"}), encoding="utf-8")
    idx = _index(fake_env)
    idx.refresh()
    names = {r.sid: r.display_name for r in idx.rows()}
    assert names == {"t1": "From titles", "t2": "Custom", "t3": "AI three", "t4": "x" * 60, "t5": "(untitled)"}


def test_display_name_live_registry_names(fake_env):
    cwd = fake_env.root / "cc"
    write_claude_session(fake_env, cwd, "n1", [_u("ask", cwd), {"type": "ai-title", "aiTitle": "AI"}])
    write_claude_session(fake_env, cwd, "n2", [_u("ask", cwd), {"type": "ai-title", "aiTitle": "AI"}])
    write_claude_session(fake_env, cwd, "n3", [_u("ask", cwd)])
    write_claude_session(fake_env, cwd, "n4", [_u("ask", cwd), {"type": "custom-title", "customTitle": "Custom"}])
    write_claude_session(fake_env, cwd, "n5", [_u("first ask five", cwd)])
    regs = {"n1": (11, "Named live", True), "n2": (12, "Reg name", False), "n3": (13, "Reg three", False),
            "n4": (14, "Named live", True), "n5": (15, None, False)}
    live = []
    for sid, (pid, name, named) in regs.items():
        _register(fake_env, pid, sid, cwd, name)
        # the snapshot's own name falls back to the folder ("cc"); the index must not use it
        live.append({"session_id": sid, "pid": pid, "name": name or "cc", "named": named, "cwd": str(cwd)})
    idx = _index(fake_env, claude_live=lambda: live)
    idx.refresh()
    names = {r.sid: r.display_name for r in idx.rows()}
    assert names == {"n1": "Named live", "n2": "AI", "n3": "Reg three", "n4": "Custom", "n5": "first ask five"}


def test_rows_filter_matches_name_path_and_first_ask(fake_env):
    net, halo = fake_env.root / "work" / "net", fake_env.root / "work" / "halo"
    write_claude_session(fake_env, net, "f1", [_u("set up PXE", net), {"type": "custom-title", "customTitle": "Boot Box"}])
    write_claude_session(fake_env, halo, "f2", [_u("ticket triage", halo)])
    idx = _index(fake_env)
    idx.refresh()
    assert [r.sid for r in idx.rows("boot box")] == ["f1"]
    assert [r.sid for r in idx.rows("HALO")] == ["f2"]
    assert [r.sid for r in idx.rows("pxe")] == ["f1"]
    assert idx.rows("nothing matches") == []


def test_two_sessions_same_name_both_listed(fake_env):
    cwd = fake_env.root / "cc"
    for sid in ("d1", "d2"):
        write_claude_session(fake_env, cwd, sid, [_u("ask", cwd), {"type": "custom-title", "customTitle": "Same"}])
    idx = _index(fake_env)
    idx.refresh()
    assert sorted(r.sid for r in idx.rows("Same")) == ["d1", "d2"]


def test_same_sid_in_two_folders_newest_wins_and_is_stable(fake_env):
    a, b = fake_env.root / "work" / "net", fake_env.root / "work" / "halo"
    older = write_claude_session(fake_env, a, "dup", [_u("old copy", a)])
    newer = write_claude_session(fake_env, b, "dup", [_u("new copy", b)])
    _touch(older, T0 - 600)
    _touch(newer, T0 - 60)
    idx = _index(fake_env)
    assert idx.refresh() == 1
    assert idx.row("claude", "dup").first_ask == "new copy"
    assert idx.refresh() == 0
    assert idx.row("claude", "dup").cwd == str(b)


def test_old_schema_version_is_rebuilt(fake_env):
    import sqlite3
    db = fake_env.data_dir / "index.sqlite"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE sessions (store TEXT, sid TEXT, path TEXT, PRIMARY KEY(store, sid))")
    con.execute("INSERT INTO sessions VALUES ('claude', 'stale', 'x')")
    con.commit()
    con.close()
    cwd = fake_env.root / "cc"
    write_claude_session(fake_env, cwd, "s1", [_u("ask", cwd)])
    idx = _index(fake_env)
    assert idx.refresh() == 1 and [r.sid for r in idx.rows()] == ["s1"]
    assert idx._db.execute("PRAGMA user_version").fetchone()[0] == index_mod._SCHEMA_VERSION


def test_refresh_error_rolls_back(fake_env, monkeypatch):
    cwd = fake_env.root / "cc"
    for sid in ("r1", "r2"):
        write_claude_session(fake_env, cwd, sid, [_u("ask", cwd)])
    idx = _index(fake_env)
    real = idx._index_file
    calls = []

    def flaky(store, path, *rest):
        calls.append(path.stem)
        if len(calls) == 2:
            raise RuntimeError("boom")
        return real(store, path, *rest)

    monkeypatch.setattr(idx, "_index_file", flaky)
    with pytest.raises(RuntimeError):
        idx.refresh()
    assert idx.rows() == []  # the first file's insert was rolled back too


def test_cwd_missing_flags_row(fake_env):
    gone = fake_env.root / "work" / "deleted-project"
    write_claude_session(fake_env, gone, "m1", [_u("ask", gone)])
    idx = _index(fake_env)
    idx.refresh()
    r = idx.row("claude", "m1")
    assert r is not None and r.cwd == str(gone) and r.cwd_exists is False


def test_vanished_files_are_deleted(fake_env):
    cwd = fake_env.root / "cc"
    p = write_claude_session(fake_env, cwd, "v1", [_u("ask", cwd)])
    write_claude_session(fake_env, cwd, "v2", [_u("ask", cwd)])
    idx = _index(fake_env)
    idx.refresh()
    p.unlink()
    assert idx.refresh() == 1
    assert idx.row("claude", "v1") is None and [r.sid for r in idx.rows()] == ["v2"]


def test_live_pid_from_registry(fake_env, monkeypatch):
    cwd = fake_env.root / "cc"
    write_claude_session(fake_env, cwd, "l1", [_u("ask", cwd)])
    write_claude_session(fake_env, cwd, "l2", [_u("ask", cwd)])
    write_claude_session(fake_env, cwd, "l3", [_u("untitled live ask", cwd)])
    (fake_env.claude_home / "sessions" / "6666.json").write_text(json.dumps({
        "pid": 6666, "sessionId": "l3", "cwd": str(cwd), "kind": "interactive", "procStart": "88"}), encoding="utf-8")
    (fake_env.claude_home / "sessions" / "4242.json").write_text(json.dumps({
        "pid": 4242, "sessionId": "l1", "cwd": str(cwd), "kind": "interactive", "procStart": "77",
        "name": "Live one", "nameSource": "user", "status": "idle"}), encoding="utf-8")
    (fake_env.claude_home / "sessions" / "5555.json").write_text(json.dumps({
        "pid": 5555, "sessionId": "l2", "cwd": str(cwd), "kind": "interactive", "procStart": "1"}), encoding="utf-8")
    monkeypatch.setattr(index_mod, "proc_start_filetime", {4242: 77, 6666: 88}.get)
    idx = Index(fake_env, pid_exists=lambda pid: False)
    idx.refresh()
    assert idx.row("claude", "l1").live_pid == 4242 and idx.row("claude", "l1").display_name == "Live one"
    assert idx.row("claude", "l2").live_pid is None and idx.row("claude", "l2").display_name == "ask"
    # live without any name: the first ask, not the folder name the snapshot falls back to
    l3 = idx.row("claude", "l3")
    assert l3.live_pid == 6666 and l3.display_name == "untitled live ask"


def test_claude_registry_live_matches_proc_start_and_reads_no_transcript(fake_env, monkeypatch):
    reg = fake_env.claude_home / "sessions"
    rec = {"pid": 4242, "sessionId": "live", "cwd": r"C:\x", "kind": "interactive", "procStart": "77",
           "name": "Named", "nameSource": "user"}
    (reg / "4242.json").write_text(json.dumps(rec), encoding="utf-8")
    reused = dict(rec, pid=5555, sessionId="reused", procStart="1")  # pid running, but a different process
    (reg / "5555.json").write_text(json.dumps(reused), encoding="utf-8")
    (reg / "6666.json").write_text(json.dumps(dict(rec, pid=6666, sessionId="bg", kind="background")),
                                   encoding="utf-8")
    (reg / "4242.key").write_text("x", encoding="utf-8")
    monkeypatch.setattr(index_mod, "proc_start_filetime", {4242: 77, 5555: 99, 6666: 77}.get)
    opened = []
    real_open = pathlib.Path.open
    monkeypatch.setattr(pathlib.Path, "open", lambda self, *a, **k: opened.append(self.name) or real_open(self, *a, **k))
    live = index_mod.claude_registry_live(fake_env.claude_home)
    assert live == [{"session_id": "live", "pid": 4242, "cwd": r"C:\x", "named": True}]
    assert sorted(opened) == ["4242.json", "5555.json", "6666.json"]  # registry records only, no transcript
    assert index_mod.claude_registry_live(fake_env.root / "nowhere") is None


def test_index_default_live_source_is_the_registry(fake_env, monkeypatch):
    cwd = fake_env.root / "cc"
    write_claude_session(fake_env, cwd, "l1", [_u("ask", cwd)])
    _register(fake_env, 4242, "l1", cwd)
    monkeypatch.setattr(index_mod, "proc_start_filetime", lambda pid: 1)
    idx = Index(fake_env, pid_exists=lambda pid: False)
    idx.refresh()
    assert idx.row("claude", "l1").live_pid == 4242


def test_qwen_live_pid_from_registry(fake_env):
    wd = fake_env.root / "home" / "ai"
    write_qwen_session(fake_env, wd, "q1", [_q_user("hi")])
    write_qwen_session(fake_env, wd, "q2", [_q_user("hi")])
    reg = fake_env.qwen_home / "sessions"
    (reg / "900.json").write_text(json.dumps({"pid": 900, "sessionId": "q1", "cwd": str(wd)}), encoding="utf-8")
    (reg / "901.json").write_text(json.dumps({"pid": 901, "sessionId": "q2", "cwd": str(wd)}), encoding="utf-8")
    (reg / "bad.json").write_text("{not json", encoding="utf-8")
    idx = _index(fake_env, qwen_alive=lambda pid: pid == 900)
    idx.refresh()
    assert idx.row("qwen", "q1").live_pid == 900 and idx.row("qwen", "q2").live_pid is None


def test_live_first_within_equal_minute(fake_env):
    cwd = fake_env.root / "cc"
    newer = write_claude_session(fake_env, cwd, "s1", [_u("ask", cwd)])
    older = write_claude_session(fake_env, cwd, "s2", [_u("ask", cwd)])
    old_minute = write_claude_session(fake_env, cwd, "s3", [_u("ask", cwd)])
    base = (T0 // 60) * 60
    _touch(newer, base + 50)
    _touch(older, base + 10)
    _touch(old_minute, base - 30)
    live = [{"session_id": sid, "pid": n, "name": "", "named": False, "cwd": str(cwd)} for n, sid in ((1, "s2"), (2, "s3"))]
    idx = _index(fake_env, claude_live=lambda: live)
    idx.refresh()
    assert [r.sid for r in idx.rows()] == ["s2", "s1", "s3"]


def test_suggest_cwd_sessions_excluded(fake_env):
    suggest = fake_env.data_dir / "suggest-cwd"
    write_claude_session(fake_env, suggest, "x1", [_u("suggest a folder", suggest)])
    write_qwen_session(fake_env, suggest, "x2", [_q_user("suggest a folder")])
    cwd = fake_env.root / "cc"
    write_claude_session(fake_env, cwd, "k1", [_u("ask", cwd)])
    idx = _index(fake_env)
    idx.refresh()
    assert [r.sid for r in idx.rows()] == ["k1"]
    # sanity: the excluded folder name really is the Claude encoding of data_dir/suggest-cwd
    assert (fake_env.claude_home / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(suggest)) / "x1.jsonl").is_file()


def test_record_launch_sets_wrapper_and_survives_refresh(fake_env):
    cwd = fake_env.root / "cc"
    p = write_claude_session(fake_env, cwd, "w1", [_u("ask", cwd)])
    idx = _index(fake_env)
    idx.refresh()
    idx.record_launch("claude", "w1", "cc-wrap")
    _append(p, _a("more"))
    idx.refresh()
    assert idx.row("claude", "w1").wrapper == "cc-wrap"


def test_folder_stats_counts_folder_and_below(fake_env):
    work = fake_env.root / "work"
    sessions = [("a", work / "net", 100), ("b", work / "clients" / "bast" / "bifrost", 300),
                ("c", work / "halo", 200), ("d", work / "net", 400), ("e", fake_env.root / "cc", 999)]
    for sid, cwd, age in sessions:
        p = write_claude_session(fake_env, cwd, sid, [_u(f"ask {sid}", cwd)])
        _touch(p, T0 - age)
    idx = _index(fake_env)
    idx.refresh()
    st = idx.folder_stats("WORK")
    assert st == FolderStats(sessions=4, last_activity=T0 - 100, names=["ask a", "ask c", "ask b"])
    assert idx.folder_stats("work/net").sessions == 2
    assert idx.folder_stats("work/ne") == FolderStats(sessions=0, last_activity=None, names=[])


def test_refresh_reports_progress(fake_env):
    cwd = fake_env.root / "cc"
    for sid in ("p1", "p2"):
        write_claude_session(fake_env, cwd, sid, [_u("ask", cwd)])
    seen = []
    _index(fake_env).refresh(progress=lambda done, total: seen.append((done, total)))
    assert seen == [(1, 2), (2, 2)]


def test_index_persists_across_instances(fake_env):
    cwd = fake_env.root / "cc"
    write_claude_session(fake_env, cwd, "z1", [_u("ask", cwd)])
    _index(fake_env).refresh()
    idx2 = _index(fake_env)
    assert idx2.row("claude", "z1").first_ask == "ask"
    assert idx2.refresh() == 0


def test_last_activity_is_the_last_record_date_not_the_file_mtime(fake_env):
    """A transcript touched today (a copy, a sync) whose last record is a month old sorts by the record."""
    from datetime import datetime, timezone
    cwd = fake_env.root / "cc"
    old = write_claude_session(fake_env, cwd, "old", [
        _u("old work", cwd, ts="2026-10-01T10:00:00.000Z"), _a("done")])
    recent = write_claude_session(fake_env, cwd, "recent", [_u("recent work", cwd, ts="2026-11-01T10:00:00.000Z")])
    _touch(old, T0 + 5 * 86400)  # the old session's file has the newest mtime
    _touch(recent, T0)
    idx = Index(fake_env, claude_live=lambda: [], pid_exists=lambda pid: False)
    idx.refresh()
    rows = idx.rows()
    assert [r.sid for r in rows] == ["recent", "old"]
    assert rows[1].last_activity == datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc).timestamp()
    assert rows[1].mtime == T0 + 5 * 86400  # mtime itself is still recorded
    stamped = write_claude_session(fake_env, cwd, "plain", [_u("no stamps"), _a("x")])
    _touch(stamped, T0 + 100)
    idx.refresh()
    assert idx.row("claude", "plain").last_activity == T0 + 100  # no timestamp: the mtime


def test_older_schema_cache_is_dropped_and_rebuilt(fake_env):
    import sqlite3
    path = fake_env.data_dir / "index.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE sessions (store TEXT, sid TEXT, stale INTEGER)")
    db.execute("INSERT INTO sessions VALUES ('claude', 'x', 1)")
    db.execute("PRAGMA user_version = 1")
    db.commit()
    db.close()
    idx = Index(fake_env, claude_live=lambda: [], pid_exists=lambda pid: False)
    assert idx.rows() == [] and idx._db.execute("PRAGMA user_version").fetchone()[0] == index_mod._SCHEMA_VERSION == 2
