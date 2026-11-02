"""Session list: incremental transcript reads and the on-disk index.

Regression for the 12-14 s blank window at startup (810 transcripts, 1.1 GB,
11 MB of JSON to the page).
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
pytest.importorskip("webview")
import claude_sessions as cs  # noqa: E402


def user(text, cwd="C:\\work\\p"):
    return {"type": "user", "cwd": cwd, "timestamp": "2026-11-01T10:00:00Z",
            "message": {"role": "user", "content": text}}


def assistant():
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    (projects / "C--work-p").mkdir(parents=True)
    monkeypatch.setattr(cs, "_SESSION_INDEX_FILE", str(tmp_path / "index" / "session_index.json"))
    cs._SESSION_PARSE_CACHE.clear()
    yield projects
    cs._SESSION_PARSE_CACHE.clear()


def write(path, *entries, mode="a"):
    with open(path, mode, encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def one(projects):
    [s] = cs.collect_sessions(str(projects))
    return s


def test_growing_transcript_is_read_incrementally(env, monkeypatch):
    t = env / "C--work-p" / "s1.jsonl"
    write(t, user("first question"), assistant())
    s = one(env)
    assert (s["user_msgs"], s["assistant_msgs"], s["first_user"]) == (1, 1, "first question")

    reads = []
    real = cs._scan_session
    monkeypatch.setattr(cs, "_scan_session", lambda p, scan=None: reads.append(scan and scan["offset"]) or real(p, scan))
    write(t, user("second"), assistant(), {"type": "ai-title", "aiTitle": "Named"})
    s = one(env)
    assert (s["user_msgs"], s["assistant_msgs"], s["auto_title"]) == (2, 2, "Named")
    assert s["first_user"] == "first question"
    assert reads and reads[-1] > 0, "must continue from the previous offset, not re-read"


def test_rewritten_transcript_is_read_from_scratch(env):
    t = env / "C--work-p" / "s1.jsonl"
    write(t, user("a"), assistant(), user("b"), assistant())
    assert one(env)["user_msgs"] == 2
    write(t, user("new start"), mode="w")          # shorter file
    s = one(env)
    assert (s["user_msgs"], s["first_user"]) == (1, "new start")


def test_index_survives_a_restart_without_rereading(env, monkeypatch):
    t = env / "C--work-p" / "s1.jsonl"
    write(t, user("hello"), assistant())
    one(env)
    assert os.path.exists(cs._SESSION_INDEX_FILE)
    cs._SESSION_PARSE_CACHE.clear()                  # "restart"
    monkeypatch.setattr(cs, "_scan_session", lambda *a, **k: pytest.fail("must not re-read"))
    assert one(env)["first_user"] == "hello"


def test_first_question_is_trimmed(env):
    t = env / "C--work-p" / "s1.jsonl"
    write(t, user("x" * 5000))
    assert len(one(env)["first_user"]) == cs._FIRST_USER_MAX


def test_deleted_transcript_disappears(env):
    a = env / "C--work-p" / "a.jsonl"
    b = env / "C--work-p" / "b.jsonl"
    write(a, user("a")); write(b, user("b"))
    assert len(cs.collect_sessions(str(env))) == 2
    os.remove(b)
    assert [s["id"] for s in cs.collect_sessions(str(env))] == ["a"]
