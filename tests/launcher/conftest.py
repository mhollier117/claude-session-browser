import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, so `launcher` imports regardless of cwd

import pytest

from launcher.paths import Env

_TREE = ("work/clients/bast/bifrost", "work/net", "work/halo", "work/vendors", "home/homelab", "home/ai", "cc")


def _claude_folder(cwd: Path) -> str:
    """Claude Code's folder name for a cwd: every character outside [A-Za-z0-9] becomes '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


@pytest.fixture
def fake_env(tmp_path) -> Env:
    root = tmp_path / "root"
    for rel in _TREE:
        (root / rel).mkdir(parents=True)
    env = Env(root=root, claude_home=tmp_path / "claude", qwen_home=tmp_path / "qwen",
              data_dir=tmp_path / "data", now=lambda: 1_700_000_000.0)
    for home in (env.claude_home, env.qwen_home):
        (home / "projects").mkdir(parents=True)
        (home / "sessions").mkdir()
    return env


def write_claude_session(env: Env, cwd: Path, sid: str, records: list[dict]) -> Path:
    path = env.claude_home / "projects" / _claude_folder(cwd) / f"{sid}.jsonl"
    _write_jsonl(path, records)
    return path


def write_qwen_session(env: Env, work_dir: Path, sid: str, records: list[dict]) -> Path:
    """Qwen mirrors Claude's layout: folder is the lower-cased Claude name, transcripts live in chats/,
    with a `<sid>.runtime.json` sidecar carrying `work_dir`."""
    chats = env.qwen_home / "projects" / _claude_folder(work_dir).lower() / "chats"
    path = chats / f"{sid}.jsonl"
    _write_jsonl(path, records)
    (chats / f"{sid}.runtime.json").write_text(json.dumps({"work_dir": str(work_dir)}), encoding="utf-8")
    return path
