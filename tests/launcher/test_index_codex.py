"""The `codex` store in the launcher index: rows from `~/.codex` rollouts, titles, liveness, labels."""
import dataclasses
import json
from pathlib import Path

import pytest

import codex_store
import launcher.index as index_mod
import launcher.stores as stores
from launcher.index import Index, backend_of, type_label
from launcher_helpers import make_row, write_codex_session

SID = "019a9a9a-1111-7222-8333-444455556666"
SID2 = "019b0000-1111-7222-8333-444455556666"


def _user(text, ts="2026-11-18T09:30:00.000Z"):
    return {"timestamp": ts, "type": "event_msg", "payload": {"type": "user_message", "message": text}}


def _agent(text, ts="2026-11-18T09:31:00.000Z"):
    return {"timestamp": ts, "type": "event_msg", "payload": {"type": "agent_message", "message": text}}


def _ctx(text):
    """Injected context: a response_item with role user, never an ask."""
    return {"timestamp": "2026-11-18T09:29:35.000Z", "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}}


@pytest.fixture
def cenv(fake_env, tmp_path):
    env = dataclasses.replace(fake_env, codex_home=tmp_path / "codex")
    (env.codex_home / "sessions").mkdir(parents=True)
    return env


def _index(env, codex_live=None):
    return Index(env, claude_live=lambda: [], pid_exists=lambda pid: False,
                 codex_live=codex_live or (lambda: {}))


def test_codex_rows_indexed_with_meta_id_and_cwd(cenv):
    path = write_codex_session(cenv, SID, [_user("hi"), _agent("hello")], cwd=cenv.root / "cc")
    assert path.stem != SID and path.stem.endswith(SID)
    idx = _index(cenv)
    idx.refresh()
    [row] = idx.rows()
    assert (row.store, row.sid, row.cwd, row.backend) == ("codex", SID, str(cenv.root / "cc"), "codex")
    assert row.path == str(path) and row.cwd_exists
    assert type_label(row) == "codex"


def test_codex_subagent_and_import_rollouts_not_indexed(cenv):
    write_codex_session(cenv, SID, [_user("x")])
    write_codex_session(cenv, SID2, [_user("sub")], source={"subagent": {"thread_spawn": {}}})
    write_codex_session(cenv, "019c0000-1111-7222-8333-444455556666", [_user("vs")], source="vscode")
    imported = "019d0000-1111-7222-8333-444455556666"
    write_codex_session(cenv, imported, [_user("imp")])
    (cenv.codex_home / "external_agent_session_imports.json").write_text(
        json.dumps({"records": [{"imported_thread_id": imported}]}), encoding="utf-8")
    idx = _index(cenv)
    idx.refresh()
    assert [r.sid for r in idx.rows()] == [SID]


def test_codex_first_and_last_ask_from_user_message_events(cenv):
    write_codex_session(cenv, SID, [_ctx("<environment_context>cwd</environment_context>"), _user("fix the build"),
                                    _agent("done")])
    idx = _index(cenv)
    idx.refresh()
    [row] = idx.rows()
    assert (row.first_ask, row.last_ask, row.last_reply) == ("fix the build", "fix the build", "done")


def test_codex_counts_only_user_and_agent_message_events(cenv):
    write_codex_session(cenv, SID, [_ctx("AGENTS.md text"), _user("a"), _agent("b"),
                                    {"timestamp": "2026-11-18T09:32:00.000Z", "type": "event_msg",
                                     "payload": {"type": "token_count"}}, _user("c"), _agent("d")])
    idx = _index(cenv)
    idx.refresh()
    [row] = idx.rows()
    assert (row.user_msgs, row.assistant_msgs) == (2, 2)


def test_codex_last_activity_from_record_timestamp(cenv):
    write_codex_session(cenv, SID, [_user("a", "2026-11-18T09:30:00.000Z")])
    idx = _index(cenv)
    idx.refresh()
    assert idx.rows()[0].last_activity == stores._iso_to_epoch("2026-11-18T09:30:00.000Z")


def test_codex_display_name_from_codex_titles(cenv):
    write_codex_session(cenv, SID, [_user("fix the build")])
    (cenv.codex_home / "session_index.jsonl").write_text(
        json.dumps({"id": SID, "thread_name": "Build fix"}) + "\n", encoding="utf-8")
    idx = _index(cenv)
    idx.refresh()
    assert idx.rows()[0].display_name == "Build fix"


def test_codex_live_pid_from_live_threads(cenv, monkeypatch):
    write_codex_session(cenv, SID, [_user("x")])
    seen = []
    monkeypatch.setattr(codex_store, "live_threads", lambda home: seen.append(home) or {SID: 4242})
    idx = Index(cenv, claude_live=lambda: [], pid_exists=lambda pid: False)  # the default reader
    idx.refresh()
    assert seen and seen[0] == cenv.codex_home
    assert idx.rows()[0].live_pid == 4242


def test_codex_live_pid_injected(cenv):
    write_codex_session(cenv, SID, [_user("x")])
    idx = _index(cenv, codex_live=lambda: {SID: 7})
    idx.refresh()
    assert idx.rows()[0].live_pid == 7


def test_no_codex_home_means_no_codex_reads(fake_env, monkeypatch):
    assert fake_env.codex_home is None

    def boom(*a, **k):
        raise AssertionError("codex_store must not be read")
    for fn in ("terminal_rollouts", "titles", "live_threads"):
        monkeypatch.setattr(codex_store, fn, boom)
    idx = Index(fake_env, claude_live=lambda: [], pid_exists=lambda pid: False)
    assert idx.refresh() == 0 and idx.rows() == []


def test_unreadable_codex_home_never_fails_refresh(cenv, monkeypatch):
    write_codex_session(cenv, SID, [_user("x")])

    def boom(*a, **k):
        raise OSError("denied")
    for fn in ("terminal_rollouts", "titles", "live_threads"):
        monkeypatch.setattr(codex_store, fn, boom)
    idx = Index(cenv, claude_live=lambda: [], pid_exists=lambda pid: False)
    assert idx.refresh() == 0 and idx.rows() == []


def test_missing_codex_home_yields_no_rows(fake_env, tmp_path):
    env = dataclasses.replace(fake_env, codex_home=tmp_path / "nope")
    idx = Index(env, claude_live=lambda: [], pid_exists=lambda pid: False)
    idx.refresh()
    assert idx.rows() == []


def test_removed_rollout_row_is_deleted(cenv):
    path = write_codex_session(cenv, SID, [_user("x")])
    idx = _index(cenv)
    idx.refresh()
    path.unlink()
    assert idx.refresh() == 1 and idx.rows() == []


def test_type_label_and_backend_codex():
    assert backend_of("codex", None, None) == "codex"
    assert backend_of("codex", "clauded", "glm-4") == "codex"
    row = make_row(None, "n", 0.0)
    row.store, row.backend, row.wrapper = "codex", "codex", "claude6"
    assert type_label(row) == "codex"
    assert index_mod.backend_label("codex") == "codex" and "codex" in index_mod.BACKENDS


def test_index_file_is_v4(tmp_path):
    assert index_mod.index_file(tmp_path).name == "index-v4.sqlite"


def test_count_messages_supports_codex(cenv):
    path = write_codex_session(cenv, SID, [_user("a"), _agent("b")])
    users, assistants, end, custom, ai = stores.count_messages(path, "codex")
    assert (users, assistants, custom, ai) == (1, 1, None, None) and end == path.stat().st_size


def test_read_codex_facts(cenv):
    path = write_codex_session(cenv, SID, [_user("a"), _agent("b")], cwd="D:/x")
    facts = stores.read_codex(path)
    assert (facts.store, facts.sid, facts.cwd, facts.user_msgs, facts.assistant_msgs) == ("codex", SID, "D:/x", 1, 1)


# ---------------------------------------------------------------- codex-cli 0.170: item_completed records

FIXTURE = Path(__file__).parent / "fixtures" / "codex-0.170-items.jsonl"


def _items():
    """Real codex-cli 0.170 records (ids and text replaced): a UserMessage, a commentary AgentMessage and a
    final_answer AgentMessage, each an `event_msg` / `item_completed`."""
    return [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_codex_item_completed_records_are_asks_and_replies(cenv):
    path = write_codex_session(cenv, SID, _items())
    facts = stores.read_codex(path)
    assert facts.first_ask == "fix the failing build please" and facts.last_ask == "fix the failing build please"
    assert facts.last_reply == "The build is fixed: a missing import."
    assert (facts.user_msgs, facts.assistant_msgs) == (1, 1)   # the commentary message is not a reply
    users, assistants, end, _, _ = stores.count_messages(path, "codex")
    assert (users, assistants, end) == (1, 1, path.stat().st_size)


def test_codex_commentary_never_the_last_reply(cenv):
    final, commentary = _items()[2], _items()[1]
    path = write_codex_session(cenv, SID, [_items()[0], final, commentary])
    assert stores.read_codex(path).last_reply == "The build is fixed: a missing import."


def test_codex_old_shape_commentary_phase_not_a_reply(cenv):
    commentary = {"timestamp": "2026-11-18T09:31:30.000Z", "type": "event_msg",
                  "payload": {"type": "agent_message", "message": "working on it", "phase": "commentary"}}
    path = write_codex_session(cenv, SID, [_user("a"), _agent("b"), commentary])
    facts = stores.read_codex(path)
    assert (facts.last_reply, facts.assistant_msgs) == ("b", 1)


def test_codex_item_completed_parser_shapes():
    rec = {"type": "event_msg", "payload": {"type": "item_completed", "item": {
        "type": "UserMessage", "content": [{"type": "text", "text": "one"}, {"type": "image"},
                                           {"type": "text", "text": "two"}]}}}
    assert stores._codex_record(rec) == ("user", "one\ntwo")
    rec["payload"]["item"] = {"type": "AgentMessage", "content": [{"type": "Text", "text": "x"}], "phase": None}
    assert stores._codex_record(rec) == ("assistant", "x")
    rec["payload"]["item"] = {"type": "AgentMessage", "content": [{"type": "Text", "text": "x"}],
                              "phase": "commentary"}
    assert stores._codex_record(rec) is None
    rec["payload"]["item"] = {"type": "Reasoning", "content": [{"type": "text", "text": "x"}]}
    assert stores._codex_record(rec) is None
    assert stores._codex_record({"type": "response_item", "payload": rec["payload"]}) is None


def test_codex_prefilter_lets_both_shapes_through():
    for line in FIXTURE.read_bytes().splitlines():
        assert stores._TYPE_RES["codex"].search(line)
    assert stores._TYPE_RES["codex"].search(b'{"type":"event_msg","payload":{"type":"user_message"}}')


def test_codex_head_read_is_capped(cenv, monkeypatch):
    """A rollout without any ask never reads the whole file looking for one."""
    filler = [{"timestamp": "2026-11-18T09:30:00.000Z", "type": "event_msg",
               "payload": {"type": "token_count", "pad": "x" * 2000}} for _ in range(200)]
    path = write_codex_session(cenv, SID, filler + [_user("late ask")])
    monkeypatch.setitem(stores._HEAD_CAP, "codex", 65_536)
    reads = []
    real = stores._head_lines
    monkeypatch.setattr(stores, "_head_lines", lambda p, size, n: reads.append(n) or real(p, size, n))
    facts = stores.read_codex(path, count=False)
    assert facts.first_ask == "" and max(reads) <= 65_536
    # a first ask inside the cap is still found
    path = write_codex_session(cenv, SID2, [_user("early ask")] + filler)
    assert stores.read_codex(path, count=False).first_ask == "early ask"
