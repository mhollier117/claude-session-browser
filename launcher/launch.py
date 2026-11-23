"""Launch commands: what to run, and where, to resume a session or start a new one.

The result is data, not a shell string: the PowerShell function `cd`s to `cwd` and splats `argv` (wrapper name
first), so nothing here ever quotes or escapes.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import termfocus

from .index import LAUNCHER_RE, Row

# claudeq and claudez run Claude Code against a self-hosted Qwen model and Z.ai GLM; qwen is Qwen Code (another tool)
WRAPPERS = ("claude", "clauded", "claude6", "claudeb", "claudew", "claudeq", "claudez", "qwen")
_DEFAULT = "clauded"  # the user's alias: claude --dangerously-skip-permissions
BYPASS_FLAG = "--dangerously-skip-permissions"
CODEX_BYPASS_FLAG = "--dangerously-bypass-approvals-and-sandbox"  # the Codex CLI's own bypass; ctrl-p drops it
# clauded, claude6 and claudew pass the bypass flag inside their profile functions, so asking permissions means the
# plain `claude`; claudeq and claudez pass every argument through to claude and never add it themselves, so bypass
# mode appends it after their name; claude and claudeb never bypass, and qwen is another tool.
_BUILT_IN_BYPASS = ("clauded", "claude6", "claudew")
_PASS_THROUGH = ("claudeq", "claudez")
# the wrapper a Claude session on another backend (index.Row.backend) always resumes with
BACKEND_WRAPPER = {"glm": "claudez", "qwen": "claudeq"}


# Claude Code's command line (`claude --help`, 2.1.x), for repeating a session's launch arguments on resume.
# Options of one launch only, dropped (with their value): resume/continue/new session id, the name, print and
# background/cloud/desktop modes, a new worktree, help/version.
_DROP_WITH_VALUE = {"--session-id", "-n", "--name"}
_DROP_OPTIONAL_VALUE = {"-r", "--resume", "--cloud", "--teleport", "--from-pr", "-w", "--worktree"}
_DROP_FLAGS = {"-c", "--continue", "--fork-session", "-p", "--print", "--bg", "--background", "--desktop",
               "-h", "--help", "-v", "--version"}
# options that take several values (every bare word after them belongs to them)
_MULTI_VALUE = {"--add-dir", "--allowedTools", "--allowed-tools", "--betas", "--disallowedTools", "--disallowed-tools",
                "--file", "--mcp-config", "--tools"}
# options without a value: a bare word after one is the initial prompt (the profile's --nb included)
_FLAGS = {"--allow-dangerously-skip-permissions", "--ax-screen-reader", "--bare", "--brief", "--chrome",
          "--dangerously-skip-permissions", "--disable-slash-commands", "--exclude-dynamic-system-prompt-sections",
          "--forward-subagent-text", "--ide", "--include-hook-events", "--include-partial-messages", "--no-chrome",
          "--no-session-persistence", "--replay-user-messages", "--restricted", "--safe-mode", "--strict-mcp-config",
          "--tmux", "--verbose", "--nb", "-nb"}


def relaunch_args(args: list[str]) -> list[str]:
    """The arguments a session was launched with, as a resume repeats them: every option and its value, without
    the options of that one launch (`_DROP_*`: an old resume, -c, --session-id, -n, -p, ...) and without the
    initial prompt (a bare word that belongs to no option). Any other option, including one this table doesn't
    know yet, keeps the bare word after it as its value, so nothing of a newer option is lost."""
    out: list[str] = []
    i, multi = 0, False
    while i < len(args):
        a = args[i]
        nxt = args[i + 1] if i + 1 < len(args) else None
        bare_next = nxt is not None and not nxt.startswith("-")
        if a.startswith("-") and a != "-":
            multi = False
            name, has_eq = a.split("=", 1)[0], "=" in a
            if name in _DROP_FLAGS:
                i += 1
            elif name in _DROP_WITH_VALUE or name in _DROP_OPTIONAL_VALUE:
                i += 1 if has_eq or not bare_next else 2
            elif has_eq or name in _FLAGS:
                out.append(a)
                i += 1
            elif name in _MULTI_VALUE:
                out.append(a)
                multi = True
                i += 1
            else:  # one value (or an option this table doesn't know): keep the word after it
                out.append(a)
                if bare_next:
                    out.append(nxt)
                    i += 2
                else:
                    i += 1
            continue
        if multi:
            out.append(a)
        i += 1  # else: the initial prompt, not repeated
    return out


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
    """Resume `row`. A Codex session resumes with `codex [bypass flag] resume <sid>` (ctrl-p drops the flag). A Qwen
    Code session always resumes with `qwen`. A Claude session whose last launch was seen
    live with its arguments (`row.launch_args`, recorded from the profile's marker) resumes exactly as launched:
    `<that alias> <its arguments, relaunch_args> -r <sid>`, any claude* alias, no table; `ask_permissions` adds
    `--nb`. Otherwise the explicit `wrapper` (ctrl-w);
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
    if row.store == "codex":  # Codex CLI: no wrappers; resume by id (an explicit wrapper does not apply)
        argv = ["codex"] if ask_permissions else ["codex", CODEX_BYPASS_FLAG]
        return Launch(cwd, [*argv, "resume", row.sid], warnings)
    if row.store == "qwen":
        chosen = "qwen"
    elif (wrapper is None and getattr(row, "launch_args", None) is not None and row.wrapper
          and LAUNCHER_RE.match(row.wrapper)):
        args = relaunch_args(row.launch_args)
        if ask_permissions and not any(a in ("--nb", "-nb") for a in args):
            args.append("--nb")
        return Launch(cwd, [row.wrapper, *args, "-r", row.sid], warnings)
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
