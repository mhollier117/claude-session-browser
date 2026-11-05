import os
from pathlib import Path

import pytest

from launcher.index import Index
from launcher.suggest_prompt import SYSTEM_PROMPT, build_user_message
from launcher_helpers import write_claude_session

_SPEC = Path(__file__).resolve().parents[2] / "docs" / "superpowers" / "specs" / "2026-11-05-session-launcher-design.md"


def _session(env, cwd, sid, name, mtime):
    path = write_claude_session(env, cwd, sid, [
        {"type": "user", "cwd": str(cwd), "timestamp": "2026-11-01T10:00:00Z",
         "message": {"role": "user", "content": name}}])
    os.utime(path, (mtime, mtime))


@pytest.fixture
def index(fake_env):
    idx = Index(fake_env, claude_live=lambda: [])
    yield idx
    idx.close()


def test_system_prompt_is_the_spec_block_verbatim():
    text = _SPEC.read_text(encoding="utf-8")
    start = text.index("You place a new coding-assistant session")
    end = text.index("\n```", start)
    assert SYSTEM_PROMPT == text[start:end]
    assert "Decide in this order:" in SYSTEM_PROMPT


def test_user_message_includes_empty_buckets_and_recent_names(fake_env, index):
    r, now = fake_env.root, fake_env.now()
    deep = r / "work" / "clients" / "bast" / "bifrost" / "stackjack_rip_qwen"
    deep.mkdir()
    _session(fake_env, deep, "s1", "stackjack rip planning", now - 2 * 3600)
    _session(fake_env, deep, "s2", "token refresh retry", now - 3 * 86400)
    _session(fake_env, r / "home" / "homelab", "s3", "homelab pxe boot order", now - 600)
    index.refresh()

    msg = build_user_message(fake_env, index, "  Keeper   SSO rollout ")
    lines = msg.splitlines()
    assert lines[0] == "Session name: Keeper SSO rollout"
    assert "work\\vendors · 0 · —" in lines  # empty bucket listed
    assert "home\\ai · 0 · —" in lines
    assert ('work\\clients\\bast\\bifrost\\stackjack_rip_qwen · 2 · 2h · "stackjack rip planning", '
            '"token refresh retry"') in lines  # depth 4 below work is listed
    assert "work · 2 · 2h · " + '"stackjack rip planning", "token refresh retry"' in lines
    assert "cc · 0 · —" in lines
    head = lines.index("Most recent 15 sessions overall (name → folder):")
    assert lines[head + 1:head + 4] == [
        '"homelab pxe boot order" → home\\homelab',
        '"stackjack rip planning" → work\\clients\\bast\\bifrost\\stackjack_rip_qwen',
        '"token refresh retry" → work\\clients\\bast\\bifrost\\stackjack_rip_qwen',
    ]


def test_user_message_depth_limits(fake_env, index):
    r = fake_env.root
    (r / "work" / "clients" / "bast" / "bifrost" / "a" / "too_deep").mkdir(parents=True)
    (r / "cc" / "reorg" / "pkg" / "too_deep").mkdir(parents=True)
    (r / "cc" / ".git").mkdir()
    (r / "cc" / "node_modules").mkdir()
    (r / "other_area").mkdir()
    msg = build_user_message(fake_env, index, "x")
    assert "work\\clients\\bast\\bifrost\\a · 0 · —" in msg
    assert "too_deep" not in msg
    assert "cc\\reorg\\pkg · 0 · —" in msg
    assert ".git" not in msg and "node_modules" not in msg and "other_area" not in msg


def test_user_message_cap_drops_deep_empty_folders_first(fake_env, index):
    r, now = fake_env.root, fake_env.now()
    for i in range(5):
        (r / "work" / "net" / f"empty{i}").mkdir()
    busy = r / "work" / "halo" / "busy"
    busy.mkdir()
    _session(fake_env, busy, "s1", "halo runbook", now - 60)
    index.refresh()
    full = build_user_message(fake_env, index, "x").splitlines()
    tree_lines = full[3:full.index("", 3)]
    # droppable empties: empty0..4, clients\bast, clients\bast\bifrost (7); the busy folder must survive
    capped = build_user_message(fake_env, index, "x", max_lines=len(tree_lines) - 7)
    assert "empty" not in capped and "bifrost" not in capped
    one_more = build_user_message(fake_env, index, "x", max_lines=len(tree_lines) - 1)
    assert "bifrost" not in one_more and "work\\clients\\bast ·" in one_more  # deepest empty goes first
    assert "work\\halo\\busy · 1" in capped
    assert "work\\vendors · 0 · —" in capped and "home\\ai · 0 · —" in capped  # buckets always stay
