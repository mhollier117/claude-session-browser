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
_DEFAULT = "clauded"  # the user's alias: claude --dangerously-skip-permissions
# ctrl-p ("ask permissions"): the same launch without --dangerously-skip-permissions. clauded, claude6 and claudew
# all pass that flag inside their profile functions, so the plain `claude` is the non-bypass form; claudeb and claude
# never bypass, and qwen is another tool.
_WITHOUT_BYPASS = {"clauded": "claude", "claude6": "claude", "claudew": "claude"}


@dataclass
class Launch:
    cwd: Path
    argv: list[str]  # wrapper name (a PowerShell function or executable) followed by its flags
    warnings: list[str] = field(default_factory=list)


def default_wrapper(root: Path, cwd: Path) -> str:
    """`clauded` everywhere (the launcher always starts in bypass mode; ctrl-p asks for permissions instead)."""
    return _DEFAULT


def without_bypass(wrapper: str) -> str:
    """The wrapper that runs the same launch with permission prompts (see `_WITHOUT_BYPASS`)."""
    return _WITHOUT_BYPASS.get(wrapper, wrapper)


def resume_command(row: Row, wrapper: str | None, *, root: Path) -> Launch:
    """Resume `row`. A Qwen session always resumes with `qwen`; otherwise the explicit `wrapper`, then the account
    wrapper that last launched it (claude6, claudeb, claudew), then `clauded`. A session last launched with plain
    `claude` resumes with `clauded` too: plain `claude` is what ctrl-p picks, per launch."""
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
        last = row.wrapper if row.wrapper not in (None, "", "claude") else None
        chosen = wrapper or last or default_wrapper(root, cwd)
    return Launch(cwd, [chosen, "-r", row.sid], warnings)


def new_command(cwd: Path, name: str, wrapper: str, session_id: str | None = None) -> Launch:
    """`<wrapper> -n <name> [--session-id <id>]`; Qwen Code has no name flag, so `qwen` gets no `-n`."""
    argv = [wrapper] if wrapper == "qwen" else [wrapper, "-n", name]
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
