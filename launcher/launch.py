"""Launch commands: what to run, and where, to resume a session or start a new one.

The result is data, not a shell string: the PowerShell function `cd`s to `cwd` and splats `argv` (wrapper name
first), so nothing here ever quotes or escapes.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import termfocus

from .index import Row

WRAPPERS = ("claude", "clauded", "claude6", "claudeb", "claudew", "qwen")
_CC_AREA = "cc"
_DEFAULT = "clauded"


@dataclass
class Launch:
    cwd: Path
    argv: list[str]  # wrapper name (a PowerShell function or executable) followed by its flags
    warnings: list[str] = field(default_factory=list)


def default_wrapper(root: Path, cwd: Path) -> str:
    """`claude` for the `cc` area (tooling for Claude Code itself), `clauded` everywhere else."""
    rel = [p.lower() for p in Path(cwd).parts]
    base = [p.lower() for p in Path(root).parts]  # case-insensitive, whole path components (Windows paths)
    area = rel[len(base):len(base) + 1] if rel[:len(base)] == base else []
    return "claude" if area == [_CC_AREA] else _DEFAULT


def resume_command(row: Row, wrapper: str | None, *, root: Path | None = None) -> Launch:
    """Resume `row`. A Qwen session always resumes with `qwen`; otherwise the explicit `wrapper`, then the one
    that last launched it, then the folder's default (the general default when no `root` is given)."""
    warnings: list[str] = []
    if row.cwd:
        cwd = Path(row.cwd)
        if not row.cwd_exists:
            warnings.append(f"folder no longer exists: {row.cwd}")
    else:
        cwd = Path(".")
        warnings.append("session has no recorded folder")
    if row.store == "qwen":
        chosen = "qwen"
    else:
        chosen = wrapper or row.wrapper or (default_wrapper(root, cwd) if root else _DEFAULT)
    return Launch(cwd, [chosen, "-r", row.sid], warnings)


def new_command(cwd: Path, name: str, wrapper: str, session_id: str | None = None) -> Launch:
    argv = [wrapper, "-n", name]
    if session_id:
        argv += ["--session-id", session_id]
    return Launch(Path(cwd), argv, [])


def to_shell_result(launch: Launch) -> str:
    return json.dumps({"cd": str(launch.cwd), "run": launch.argv, "warnings": launch.warnings},
                      ensure_ascii=False)


def focus_live(row: Row) -> dict:
    """Bring the terminal window/tab of a live session to the front (see `termfocus.focus_session`)."""
    if row.live_pid is None:
        return {"ok": False, "window": False, "tab": "none", "via": "", "ms": 0, "error": "session is not live",
                "note": ""}
    return termfocus.focus_session(row.live_pid, row.cwd)
