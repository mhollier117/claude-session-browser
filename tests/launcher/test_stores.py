import json

from launcher.stores import SessionFacts, clean_user_text, count_messages, full_last_reply, is_top_level_claude, read_claude, read_qwen
from launcher_helpers import write_claude_session, write_qwen_session


def _u(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _a(text):
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def test_claude_first_and_last_ask_skip_tool_results(fake_env):
    p = write_claude_session(fake_env, fake_env.root / "work" / "net", "s1", [
        {"type": "user", "cwd": str(fake_env.root / "work" / "net"), "timestamp": "2026-11-01T10:00:00Z",
         "message": {"role": "user", "content": "set up PXE <system-reminder>x</system-reminder>"}},
        _a("Reply one."),
        _u("why no DHCP offer?"),
        _a("Add the VLAN."),
        {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}},
    ])
    f = read_claude(p)
    assert isinstance(f, SessionFacts) and f.store == "claude" and f.sid == "s1" and f.path == p
    assert (f.first_ask, f.last_ask, f.last_reply) == ("set up PXE", "why no DHCP offer?", "Add the VLAN.")
    assert f.cwd == str(fake_env.root / "work" / "net") and f.user_msgs == 2 and f.assistant_msgs == 2
    assert f.size == p.stat().st_size and f.mtime == p.stat().st_mtime and f.name is None


def test_claude_user_text_blocks_are_joined(fake_env):
    p = write_claude_session(fake_env, fake_env.root / "cc", "s2", [
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "look at"}, {"type": "image", "source": {}}, {"type": "text", "text": "this"}]}},
    ])
    assert read_claude(p).first_ask == "look at this"


def test_claude_name_custom_title_beats_ai_title(fake_env):
    cwd = fake_env.root / "cc"
    p = write_claude_session(fake_env, cwd, "s3", [
        _u("hi"), {"type": "ai-title", "aiTitle": "AI one"}, {"type": "custom-title", "customTitle": "Mine"},
        _a("yo"), {"type": "ai-title", "aiTitle": "AI later"},
    ])
    assert read_claude(p).name == "Mine"
    p2 = write_claude_session(fake_env, cwd, "s4", [_u("hi"), {"type": "ai-title", "aiTitle": "A1"},
                                                    {"type": "ai-title", "aiTitle": "A2"}])
    assert read_claude(p2).name == "A2"


def test_head_read_grows_when_first_ask_is_large(fake_env):
    big = "x" * 200_000
    p = write_claude_session(fake_env, fake_env.root / "cc", "s5", [_u(big), _a("done")])
    f = read_claude(p)
    assert f.first_ask == big and f.last_reply == "done"


def test_tail_read_grows_when_last_reply_is_large(fake_env):
    big = "y" * 600_000
    p = write_claude_session(fake_env, fake_env.root / "cc", "s6", [_u("q1"), _a("early"), _u("q2"), _a(big)])
    f = read_claude(p, tail_bytes=4096)
    assert f.last_reply == big and f.last_ask == "q2"
    assert full_last_reply(p, "claude") == big


_MB = 1024 * 1024


def _long_tool_run(env, sid="big"):
    """A 20 MB transcript: the last typed ask is 18 MB from the end (18 one-MB assistant blocks after it)."""
    recs = [_u("first"), _a("x" * (2 * _MB - 200)), _u("the last typed ask")] + \
        [_a(f"{i} " + "z" * (_MB - 100)) for i in range(18)] + [_a("final reply")]
    return write_claude_session(env, env.root / "cc", sid, recs)


def test_tail_read_stops_at_the_cap(fake_env, monkeypatch):
    import launcher.stores as stores
    p = _long_tool_run(fake_env)
    assert p.stat().st_size > 19 * _MB
    reads = []
    real = stores._tail_lines
    monkeypatch.setattr(stores, "_tail_lines", lambda path, size, n: reads.append(n) or real(path, size, n))
    f = read_claude(p, count=False)
    assert f.last_reply == "final reply"
    assert f.last_ask == ""  # not within the last 16 MB: unknown, not read whole
    assert f.first_ask == "first"
    assert max(reads) == stores.TAIL_MAX_BYTES == 16 * _MB


def test_tail_skips_partial_first_line(fake_env):
    recs = [_u("q1"), _a("r1")] + [_u(f"filler {i}" * 50) for i in range(40)] + [_u("final q"), _a("final r")]
    p = write_claude_session(fake_env, fake_env.root / "cc", "s7", recs)
    f = read_claude(p, tail_bytes=300)
    assert (f.first_ask, f.last_ask, f.last_reply) == ("q1", "final q", "final r")


def test_empty_and_malformed_lines_tolerated(fake_env):
    p = write_claude_session(fake_env, fake_env.root / "cc", "s8", [_u("only")])
    with open(p, "a", encoding="utf-8", newline="\n") as fh:
        fh.write("not json\n\n")
    f = read_claude(p)
    assert (f.first_ask, f.last_ask, f.last_reply, f.assistant_msgs) == ("only", "only", "", 0)


def test_is_top_level_claude_filters_agent_and_subagent_files(fake_env):
    d = fake_env.claude_home / "projects" / "p"
    assert is_top_level_claude(d / "abc.jsonl")
    assert not is_top_level_claude(d / "agent-1234.jsonl")
    assert not is_top_level_claude(d / "abc" / "subagents" / "x.jsonl")
    assert not is_top_level_claude(d / "abc.jsonl.bak")
    assert not is_top_level_claude(d / "abc.companion.json")
    assert not is_top_level_claude(d / "abc.jsonl.bak-20261101")


def _q_user(text, cwd, provenance="real_user"):
    return {"type": "user", "provenance": provenance, "cwd": cwd, "message": {"role": "user", "parts": [{"text": text}]}}


def _q_asst(*parts):
    return {"type": "assistant", "provenance": "assistant_output", "message": {"role": "model", "parts": list(parts)}}


def test_read_qwen_uses_runtime_work_dir(fake_env):
    STALE = str(fake_env.root / "stale")
    wd = fake_env.root / "home" / "ai"
    p = write_qwen_session(fake_env, wd, "q1", [
        {"type": "system", "subtype": "attribution_snapshot"},
        _q_user("first <system-reminder>z</system-reminder> q", STALE),
        _q_asst({"text": "thinking...", "thought": True}, {"text": "Answer one."}),
        {"type": "tool_result", "message": {"role": "user", "parts": [{"functionResponse": {"name": "x"}}]}},
        _q_user("injected", STALE, provenance="system"),
        _q_user("second q", STALE),
        _q_asst({"functionCall": {"name": "x"}}),
        _q_asst({"text": "Answer two."}),
    ])
    f = read_qwen(p)
    assert f.store == "qwen" and f.sid == "q1" and f.cwd == str(wd) and f.name is None
    assert (f.first_ask, f.last_ask, f.last_reply) == ("first q", "second q", "Answer two.")
    assert (f.user_msgs, f.assistant_msgs) == (2, 2)
    assert full_last_reply(p, "qwen") == "Answer two."


def test_read_qwen_falls_back_to_record_cwd(fake_env):
    wd = fake_env.root / "home" / "ai"
    from_record = str(fake_env.root / "elsewhere" / "record")
    p = write_qwen_session(fake_env, wd, "q2", [_q_user("hi", from_record)])
    p.with_name("q2.runtime.json").unlink()
    assert read_qwen(p).cwd == from_record


def test_empty_transcript_gives_empty_facts(fake_env):
    p = write_claude_session(fake_env, fake_env.root / "cc", "e1", [])
    assert p.stat().st_size == 0
    f = read_claude(p)
    assert (f.cwd, f.name, f.first_ask, f.last_ask, f.last_reply) == (None, None, "", "", "")
    assert (f.user_msgs, f.assistant_msgs, f.size) == (0, 0, 0)


def test_tail_window_inside_multibyte_character(fake_env):
    reply = "é…" * 3000  # 5 bytes per pair in UTF-8
    path = fake_env.claude_home / "projects" / "mb" / "m1.jsonl"
    path.parent.mkdir(parents=True)
    records = [_u("q1"), _a("r1"), _u("qé…"), _a(reply)]
    lines = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)  # raw UTF-8, not \u escapes
    path.write_bytes(lines.encode("utf-8"))
    assert len(reply.encode("utf-8")) > 4096 * 3
    # windows of 4096..4101 bytes start at every offset of the 5-byte period inside the last record
    for tail_bytes in range(4096, 4102):
        f = read_claude(path, tail_bytes=tail_bytes)
        assert (f.last_ask, f.last_reply) == ("qé…", reply)


def test_read_with_count_false_skips_the_full_pass(fake_env, monkeypatch):
    import launcher.stores as stores
    p = write_claude_session(fake_env, fake_env.root / "cc", "c0", [_u("q1"), {"type": "ai-title", "aiTitle": "T"},
                                                                    _a("r1")])
    monkeypatch.setattr(stores, "_count", lambda *a: (_ for _ in ()).throw(AssertionError("full pass")))
    f = read_claude(p, count=False)
    assert (f.first_ask, f.last_reply, f.user_msgs, f.assistant_msgs, f.name) == ("q1", "r1", 0, 0, None)


def test_count_messages_whole_and_incremental(fake_env):
    p = write_claude_session(fake_env, fake_env.root / "cc", "c1", [
        _u("q1"), {"type": "custom-title", "customTitle": "Mine"}, _a("r1"), {"type": "ai-title", "aiTitle": "A1"}])
    u, a, end, custom, ai = count_messages(p, "claude")
    assert (u, a, end, custom, ai) == (1, 1, p.stat().st_size, "Mine", "A1")
    with open(p, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(_u("q2")) + "\n" + json.dumps(_a("r2")) + "\n")
    assert count_messages(p, "claude", end) == (1, 1, p.stat().st_size, None, None)


def test_count_messages_stops_before_a_partial_last_line(fake_env):
    p = write_claude_session(fake_env, fake_env.root / "cc", "c2", [_u("q1")])
    whole = p.stat().st_size
    with open(p, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(_a("half"))[:20])  # still being written
    assert count_messages(p, "claude") == (1, 0, whole, None, None)


def test_count_messages_qwen(fake_env):
    p = write_qwen_session(fake_env, fake_env.root / "home" / "ai", "c3", [
        _q_user("a", "x"), _q_user("inj", "x", provenance="system"), _q_asst({"text": "b"})])
    assert count_messages(p, "qwen")[:2] == (1, 1)


def test_clean_user_text():
    s = "<local-command-caveat>c</local-command-caveat><command-name>/x</command-name>\n real  <b>ask</b>\n<system-reminder>\nz\n</system-reminder>"
    assert clean_user_text(s) == "real ask"
    assert clean_user_text("<local-command-stdout>out</local-command-stdout>") == ""
    assert clean_user_text("") == ""


def test_last_ts_is_the_last_timestamp_in_the_tail_claude_and_qwen(fake_env):
    cwd = fake_env.root / "cc"
    p = write_claude_session(fake_env, cwd, "t1", [
        {"type": "user", "timestamp": "2026-11-01T10:00:00.000Z", "message": {"role": "user", "content": "hi"}},
        {"type": "assistant", "timestamp": "2026-11-03T12:30:15.500Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "yo"}]}},
        {"type": "ai-title", "aiTitle": "A title"},  # no timestamp: skipped
    ])
    from datetime import datetime, timezone
    assert read_claude(p).last_ts == datetime(2026, 11, 3, 12, 30, 15, 500000, tzinfo=timezone.utc).timestamp()
    q = write_qwen_session(fake_env, cwd, "t2", [
        {"type": "user", "provenance": "real_user", "timestamp": "2026-08-19T14:02:54.267Z",
         "message": {"role": "user", "parts": [{"text": "hello"}]}},
        {"type": "assistant", "timestamp": "2026-08-19T14:03:10.000Z",
         "message": {"role": "model", "parts": [{"text": "hi"}]}}])
    assert read_qwen(q).last_ts == datetime(2026, 8, 19, 14, 3, 10, tzinfo=timezone.utc).timestamp()


def test_last_ts_is_none_without_a_usable_timestamp(fake_env):
    cwd = fake_env.root / "cc"
    p = write_claude_session(fake_env, cwd, "t3", [_u("hi"), _a("yo")])
    assert read_claude(p).last_ts is None
    p2 = write_claude_session(fake_env, cwd, "t4", [
        {"type": "user", "timestamp": "not a date", "message": {"role": "user", "content": "hi"}}])
    assert read_claude(p2).last_ts is None

def test_last_ts_honours_an_explicit_utc_offset(fake_env):
    from datetime import datetime, timezone
    p = write_claude_session(fake_env, fake_env.root / "cc", "t5", [
        {"type": "user", "timestamp": "2026-11-03T14:30:15.500+02:00", "message": {"role": "user", "content": "hi"}}])
    assert read_claude(p).last_ts == datetime(2026, 11, 3, 12, 30, 15, 500000, tzinfo=timezone.utc).timestamp()


def test_last_ts_skips_non_string_timestamps_without_crashing(fake_env):
    from datetime import datetime, timezone
    good = "2026-11-01T10:00:00Z"
    p = write_claude_session(fake_env, fake_env.root / "cc", "t6", [
        {"type": "user", "timestamp": good, "message": {"role": "user", "content": "hi"}},
        {"type": "assistant", "timestamp": 1762000000, "message": {"role": "assistant", "content": "x"}},
        {"type": "assistant", "timestamp": None, "message": {"role": "assistant", "content": "y"}},
        {"type": "system", "timestamp": ["2026-11-09T00:00:00Z"]}])
    assert read_claude(p).last_ts == datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc).timestamp()
    p2 = write_claude_session(fake_env, fake_env.root / "cc", "t7", [
        {"type": "user", "timestamp": 12345, "message": {"role": "user", "content": "hi"}}])
    assert read_claude(p2).last_ts is None
