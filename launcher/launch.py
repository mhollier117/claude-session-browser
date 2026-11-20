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

# claudeq and claudez run Claude Code against a self-hosted Qwen model and Z.ai GLM; qwen is Qwen Code (another tool)
WRAPPERS = ("claude", "clauded", "claude6", "claudeb", "claudew", "claudeq", "claudez", "qwen")
_DEFAULT = "clauded"  # the user's alias: claude --dangerously-skip-permissions
BYPASS_FLAG = "--dangerously-skip-permissions"
# clauded, claude6 and claudew pass the bypass flag inside their profile functions, so asking permissions means the
# plain `claude`; claudeq and claudez pass every argument through to claude and never add it themselves, so bypass
# mode appends it after their name; claude and claudeb never bypass, and qwen is another tool.
_BUILT_IN_BYPASS = ("clauded", "claude6", "claudew")
_PASS_THROUGH = ("claudeq", "claudez")
# the wrapper a Claude session on another backend (index.Row.backend) always resumes with
BACKEND_WRAPPER = {"glm": "claudez", "qwen": "claudeq"}


@dataclass
class Launch:
    cwd: Path
    argv: list[str]  # wrapper name (a PowerShell function or executable) followed by its flags
    warnings: list[str] = field(default_factory=list)


def default_wrapper(root: Path, cwd: Path) -> str:
    """`clauded` everywhere (the launcher always starts in bypass mode; ctrl-p asks for permissions instead)."""
    return _DEFAULT


def wrapper_argv(wrapper: str, ask_permissions: bool = False) -> list[str]:
    """The wrapper part of a launch's argv. `ask_permissions` (ctrl-p): clauded, claude6 and claudew become the
    plain `claude`, claudeq and claudez stay as they are, the rest are unchanged. Bypass mode (the default): claudeq
    and claudez get `--dangerously-skip-permissions` after their name, every other wrapper is used as it is."""
    if ask_permissions:
        return ["claude"] if wrapper in _BUILT_IN_BYPASS else [wrapper]
    return [wrapper, BYPASS_FLAG] if wrapper in _PASS_THROUGH else [wrapper]


def resume_command(row: Row, wrapper: str | None, *, root: Path, ask_permissions: bool = False) -> Launch:
    """Resume `row`. A Qwen Code session always resumes with `qwen`. Otherwise the explicit `wrapper` (ctrl-w);
    then, for a Claude session on another backend, `claudez` (glm) or `claudeq` (qwen) whatever last launched it
    (`BACKEND_WRAPPER`); then the account wrapper that last launched it (claude6, claudeb, claudew); then `clauded`.
    A session last launched with plain `claude` resumes with `clauded` too: plain `claude` is what ctrl-p picks,
    per launch. The wrapper part of the argv comes from `wrapper_argv(chosen, ask_permissions)`."""
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
        chosen = wrapper or BACKEND_WRAPPER.get(row.backend) or last or default_wrapper(root, cwd)
    return Launch(cwd, [*wrapper_argv(chosen, ask_permissions), "-r", row.sid], warnings)


def new_command(cwd: Path, name: str, wrapper: str, session_id: str | None = None,
                ask_permissions: bool = False) -> Launch:
    """`<wrapper part> -n <name> [--session-id <id>]`, the wrapper part from `wrapper_argv`; Qwen Code has no name
    flag, so `qwen` gets no `-n`."""
    argv = wrapper_argv(wrapper, ask_permissions)
    if wrapper != "qwen":
        argv += ["-n", name]
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
