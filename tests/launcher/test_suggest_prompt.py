import os
from pathlib import Path

import pytest

from launcher.index import Index
from launcher.suggest_prompt import SYSTEM_PROMPT_V1, build_user_message_v1
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
    assert SYSTEM_PROMPT_V1 == text[start:end]
    assert "Decide in this order:" in SYSTEM_PROMPT_V1


def test_user_message_includes_empty_buckets_and_recent_names(fake_env, index):
    r, now = fake_env.root, fake_env.now()
    deep = r / "work" / "clients" / "bast" / "bifrost" / "stackjack_rip_qwen"
    deep.mkdir()
    _session(fake_env, deep, "s1", "stackjack rip planning", now - 2 * 3600)
    _session(fake_env, deep, "s2", "token refresh retry", now - 3 * 86400)
    _session(fake_env, r / "home" / "homelab", "s3", "homelab pxe boot order", now - 600)
    index.refresh()

    msg = build_user_message_v1(fake_env, index, "  Keeper   SSO rollout ")
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
    msg = build_user_message_v1(fake_env, index, "x")
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
    full = build_user_message_v1(fake_env, index, "x").splitlines()
    tree_lines = full[3:full.index("", 3)]
    # droppable empties: empty0..4, clients\bast, clients\bast\bifrost (7); the busy folder must survive
    capped = build_user_message_v1(fake_env, index, "x", max_lines=len(tree_lines) - 7)
    assert "empty" not in capped and "bifrost" not in capped
    one_more = build_user_message_v1(fake_env, index, "x", max_lines=len(tree_lines) - 1)
    assert "bifrost" not in one_more and "work\\clients\\bast ·" in one_more  # deepest empty goes first
    assert "work\\halo\\busy · 1" in capped
    assert "work\\vendors · 0 · —" in capped and "home\\ai · 0 · —" in capped  # buckets always stay


# ---- v2: numbered candidates ----------------------------------------------------------------------------------------

from launcher.index import FolderStats  # noqa: E402
from launcher.retrieve import Cand, Place, Scored  # noqa: E402
from launcher.suggest_prompt import SYSTEM_PROMPT, Choice, build_user_message  # noqa: E402


def _scored(root, rel, sessions, names):
    path = root / rel
    return Scored(Cand(rel, path, rel.count("\\") + 1, FolderStats(sessions, 1.0, list(names)), frozenset()), 1.0)


def test_v2_user_message_numbers_projects_then_places(fake_env):
    r = fake_env.root
    scored = [_scored(r, "work\\halo\\view_management", 7, ["Halo ticket list", "view sync", "third"]),
              _scored(r, "home\\ai", 2, []),  # also a place: listed once, as the place
              _scored(r, "work\\net\\cert issue", 1, ["cert"])]
    places = [Place("work\\net", r / "work" / "net", "Fortinet, VPN"), Place("home\\ai", r / "home" / "ai", "local AI"),
              Place("cc", r / "cc", "")]
    msg, choices = build_user_message("  Halo   ticket list sync bug ", scored, places)
    lines = msg.splitlines()
    assert lines[0] == "Session name: Halo ticket list sync bug"
    assert lines[lines.index("Candidates:") + 1:] == [
        '1. PROJECT  work\\halo\\view_management  · 7 sessions · recent: "Halo ticket list", "view sync"',
        "2. PROJECT  work\\net\\cert issue  · 1 sessions · recent: \"cert\"",
        "3. PLACE    work\\net  — Fortinet, VPN",
        "4. PLACE    home\\ai  — local AI",
        "5. PLACE    cc",
    ]
    assert choices == [Choice("PROJECT", "work\\halo\\view_management", r / "work" / "halo" / "view_management"),
                       Choice("PROJECT", "work\\net\\cert issue", r / "work" / "net" / "cert issue"),
                       Choice("PLACE", "work\\net", r / "work" / "net"), Choice("PLACE", "home\\ai", r / "home" / "ai"),
                       Choice("PLACE", "cc", r / "cc")]


def test_v2_user_message_without_projects(fake_env):
    msg, choices = build_user_message("SFDSDF", [], [Place("home", fake_env.root / "home", "personal")])
    assert msg.splitlines()[-1] == "1. PLACE    home  — personal" and [c.kind for c in choices] == ["PLACE"]


def test_v2_system_prompt_states_the_contract():
    assert '"choice"' in SYSTEM_PROMPT and '"new_folder"' in SYSTEM_PROMPT
    assert "never" in SYSTEM_PROMPT.lower() and "{}" in SYSTEM_PROMPT
    assert SYSTEM_PROMPT.count("Example") >= 4


def test_v2_project_line_shows_the_recent_names_that_match_best(fake_env):
    r = fake_env.root
    scored = [_scored(r, "work\\halo\\view_management", 4,
                      ["halo views", "views.portal DNS resolution", "Bastionpoint ticket list sync tool"])]
    msg, _choices = build_user_message("Halo ticket list sync bug", scored, [])
    assert msg.splitlines()[-1] == ('1. PROJECT  work\\halo\\view_management  · 4 sessions · recent: '
                                    '"Bastionpoint ticket list sync tool", "halo views"')


def test_v2_place_line_shows_sessions_started_there(fake_env):
    r = fake_env.root
    msg, _ = build_user_message("x", [], [Place("home\\ai", r / "home" / "ai", "local AI", 3, ("a b", "c")),
                                          Place("home", r / "home", "", 1, ("solo",))])
    assert msg.splitlines()[-2:] == ['1. PLACE    home\\ai  — local AI · 3 sessions here · recent: "a b", "c"',
                                     '2. PLACE    home · 1 session here · recent: "solo"']
    assert "start in the place itself" in SYSTEM_PROMPT


def test_v2_place_note_drops_a_listed_project_name(fake_env):
    r = fake_env.root
    scored = [_scored(r, "home\\traefik", 2, ["dash"])]
    places = [Place("home\\homelab", r / "home" / "homelab", "homelab: PXE, Supermicro, Traefik"),
              Place("work\\vendors", r / "work" / "vendors", "vendor tooling (Traefik, Keeper, Keepit)")]
    msg, _ = build_user_message("traefik dashboard", scored, places)
    lines = msg.splitlines()
    assert lines[-2] == "2. PLACE    home\\homelab  — homelab: PXE, Supermicro"
    assert lines[-1] == "3. PLACE    work\\vendors  — vendor tooling (Keeper, Keepit)"
    assert "merely mentions" in SYSTEM_PROMPT
