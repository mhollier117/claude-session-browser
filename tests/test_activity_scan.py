"""The buddy (and through it the Clawdmeter) must find the session that is
actually active, however many transcripts exist.

Regression: the scan used to stop after 200 files in directory order, so with
more sessions the active one was often skipped and an old, finished session
was shown ("done" / idle instead of thinking or writing).
"""

import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
pytest.importorskip("webview")
import claude_sessions as cs  # noqa: E402


@pytest.fixture
def projects(tmp_path):
    old = time.time() - 3600
    # 250 finished sessions in folders that list BEFORE the active one
    for i in range(250):
        d = tmp_path / f"a-project-{i:03d}"
        d.mkdir()
        f = d / "s.jsonl"
        f.write_text(json.dumps({"type": "assistant", "message": {
            "role": "assistant", "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "done"}]}}) + "\n")
        os.utime(f, (old, old))
    # the active session, listed last, with a tool still running
    active = tmp_path / "zzz-active-project" / "live.jsonl"
    active.parent.mkdir()
    active.write_text(json.dumps({"type": "assistant", "message": {
        "role": "assistant", "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]}}) + "\n")
    return tmp_path, active


def test_newest_mtime_finds_the_active_session(projects):
    root, active = projects
    assert cs._latest_session_mtime(str(root)) == pytest.approx(active.stat().st_mtime)


def test_status_comes_from_the_active_session(projects):
    root, _ = projects
    st = cs._latest_jsonl_status(str(root))
    assert st["internal_state"] != "done"
    assert st["last_block_type"] == "tool_use"
