"""Session-writing helpers for the launcher tests (imported by name: `from launcher_helpers import ...`)."""
import json
import re
from pathlib import Path

from launcher.paths import Env


def _claude_folder(cwd: Path) -> str:
    """Claude Code's folder name for a cwd: every character outside [A-Za-z0-9] becomes '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n": lines end in LF only, even on Windows (byte-offset tail reads depend on it)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("".join(json.dumps(r) + "\n" for r in records))


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


def make_row(cwd, name: str, last_activity: float, sid: str = "s"):
    """A synthetic index Row (no transcript behind it) for tests that only need cwd, name and activity."""
    from launcher.index import Row
    return Row(store="claude", sid=sid, cwd=None if cwd is None else str(cwd), name=None, display_name=name,
               first_ask=name, last_ask="", last_reply="", user_msgs=1, assistant_msgs=0, size=0,
               mtime=last_activity, last_activity=last_activity, live_pid=None, wrapper=None, cwd_exists=True)
