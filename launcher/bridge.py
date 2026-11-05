"""Glue between the session browser's js_api (`claude_sessions.Api`) and the launcher core.

Everything here is pure or takes its side effects as arguments (runner, clock, which), so it is tested without
pywebview; the `Api` methods only hold the Index and a lock, and call `Popen` with the argv built here.

Launching from the browser: the wrappers (`clauded`, `claude6`, ...) are PowerShell functions from the user's
profile, so a launch runs `pwsh -NoExit -EncodedCommand <base64>` of `& 'wrapper' 'arg' ...` in a new Windows
Terminal tab. `ps_command` is the one place anything is quoted; the base64 form then passes through wt's own command
line parsing (which splits at `;` and re-quotes) untouched.
"""
from __future__ import annotations

import base64
import contextlib
import re
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from .index import Index, Row, remember_launch
from .launch import WRAPPERS, Launch, default_wrapper, new_command
from .paths import Env
from .scaffold import Runner, check_name, create_folder
from .suggest import Pending, Suggestion, _resolve
from .tree import children, new_folder_kind_options

SUGGEST_TTL_S = 60.0
MODEL_IDLE_S = 1.5  # the browser starts the model stages only once the name has been left alone this long
GITHUB_ACCOUNTS = (None, "personal", "work")
_PS_QUOTES = "'‘’‚‛"  # PowerShell treats the typographic single quotes as `'` too
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


# ---- launching ------------------------------------------------------------------------------------------------------

def ps_quote(value: str) -> str:
    """`value` as a PowerShell single-quoted literal: nothing inside is expanded ($, backtick, double quotes);
    each single-quote character is doubled."""
    return "'" + "".join(c * 2 if c in _PS_QUOTES else c for c in value) + "'"


def ps_command(argv: list[str]) -> str:
    """`& 'command' 'arg' ...`: invoke `argv[0]` (a wrapper name from `launch.WRAPPERS`, never user text) with every
    argument passed as one literal string."""
    if not argv:
        raise ValueError("nothing to run")
    return "& " + " ".join(ps_quote(str(a)) for a in argv)


def encoded_command(cmd: str) -> str:
    """`cmd` as PowerShell's `-EncodedCommand` value: base64 of its UTF-16LE bytes (no `;`, `"`, spaces or backslashes)."""
    return base64.b64encode(cmd.encode("utf-16-le")).decode("ascii")


def pick_shell(which=shutil.which) -> str:
    """PowerShell 7 when it is on PATH, else Windows PowerShell."""
    return "pwsh" if which("pwsh") else "powershell"


def wt_argv(cwd: Path, argv: list[str], shell: str) -> list[str]:
    """A new tab in the most recent Windows Terminal window, in `cwd`, running `argv` through `shell`."""
    return ["wt", "-w", "0", "nt", "-d", str(cwd), *console_argv(argv, shell)]


def console_argv(argv: list[str], shell: str) -> list[str]:
    """`shell` running `argv` and staying open; also the whole command for a new console window (when Windows
    Terminal is not installed), run with cwd set."""
    return [shell, "-NoExit", "-EncodedCommand", encoded_command(ps_command(argv))]


# ---- paths ----------------------------------------------------------------------------------------------------------

def resolve_rel(root: Path, rel) -> Path:
    """`rel` ('/' or '\\' separated, relative to `root`; '' is the root) as an existing folder at or under the root,
    matched case-insensitively. Raises ValueError otherwise (including `..` and paths outside the root)."""
    if rel is None:
        return root
    if not isinstance(rel, str):
        raise ValueError(f"not a folder path: {rel!r}")
    value = rel.strip()
    if not Path(value).drive:
        value = value.strip("/\\")  # '/work/net/' is relative to the root, not to the current drive
    if value in ("", "."):
        return root
    path = _resolve(root, value)
    if path is None:
        raise ValueError(f"no such folder under the root: {rel!r}")
    return path


def rel_of(root: Path, path: Path) -> str:
    """`path` relative to `root` with '/' separators; '' for the root itself."""
    rel = Path(path).relative_to(root).as_posix()
    return "" if rel == "." else rel


# ---- serialisation --------------------------------------------------------------------------------------------------

def suggestion_dict(root: Path, s: Suggestion) -> dict:
    return {"path": rel_of(root, s.path), "new_folder": s.new_folder, "confidence": s.confidence,
            "reason": s.reason, "source": s.source}


def row_dict(row: Row) -> dict:
    """An Index row for the browser, with `tool` (claude | qwen)."""
    return {**asdict(row), "tool": row.store}


def qwen_session_dicts(rows: list[Row], when) -> list[dict]:
    """The Qwen rows among `rows` in the browser's session-list shape; `when(mtime)` formats the time column as
    the Claude rows get it."""
    return [{"id": r.sid, "auto_title": r.display_name, "display_title": r.display_name, "first_user": r.first_ask,
             "cwd": r.cwd or "", "mtime": r.mtime, "when": when(r.mtime), "user_msgs": r.user_msgs,
             "assistant_msgs": r.assistant_msgs, "total_msgs": r.user_msgs + r.assistant_msgs,
             "project": Path(r.cwd).name if r.cwd else "", "color": "", "tool": "qwen"}
            for r in rows if r.store == "qwen"]


def children_listing(env: Env, index: Index, folder: Path) -> dict:
    """One column of the New session dialog: `folder`'s sub-folders with session stats, whether a session can start
    here (not at the root), the kinds a new folder here can take (default first) and the default wrapper here."""
    return {
        "ok": True,
        "rel": rel_of(env.root, folder),
        "children": [{"name": c.name, "rel": rel_of(env.root, c.path), "kind": c.kind, "sessions": c.stats.sessions,
                      "last_activity": c.stats.last_activity, "names": list(c.stats.names)}
                     for c in children(env, index, folder)],
        "start_here": folder != env.root,
        "kind_options": new_folder_kind_options(env.root, folder),
        "default_wrapper": default_wrapper(env.root, folder),
    }


# ---- new session ----------------------------------------------------------------------------------------------------

@dataclass
class NewRequest:
    name: str  # the session name
    folder: Path  # where to start, or the parent of the new folder
    new_folder: str | None
    kind: str | None
    wrapper: str | None
    github: str | None

    @property
    def launches(self) -> bool:
        """A session starts unless the request only creates a grouping folder (container, client, organisational)."""
        return self.new_folder is None or self.kind == "project"


def parse_new(root: Path, payload) -> NewRequest:
    """Validate the dialog's `{name, rel, new_folder, kind, wrapper, github}`; raises ValueError naming the problem."""
    if not isinstance(payload, dict):
        raise ValueError("the request must be an object")
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("a session name is required")
    name = name.strip()
    if _CONTROL.search(name):
        raise ValueError("the session name must not contain control characters (line breaks, tabs)")
    folder = resolve_rel(root, payload.get("rel"))
    new_folder = payload.get("new_folder") or None
    kind = None
    if new_folder is not None:
        if not isinstance(new_folder, str):
            raise ValueError(f"invalid folder name: {new_folder!r}")
        new_folder = new_folder.strip()
        check_name(new_folder)
        options = new_folder_kind_options(root, folder)
        kind = payload.get("kind")
        if kind not in options:
            raise ValueError(f"kind must be one of {', '.join(options)} here, not {kind!r}")
        if (folder / new_folder).exists():
            raise ValueError(f"{new_folder} already exists in {rel_of(root, folder) or 'the root'}")
    elif folder == root:
        raise ValueError("choose a folder under the root to start the session in")
    github = payload.get("github") or None
    if github not in GITHUB_ACCOUNTS:
        raise ValueError(f"unknown github account: {github!r}")
    wrapper = payload.get("wrapper")
    req = NewRequest(name, folder, new_folder, kind, wrapper, github)
    if req.launches and wrapper not in WRAPPERS:
        raise ValueError(f"unknown wrapper: {wrapper!r}")
    return req


def prepare_new(env: Env, payload, runner: Runner | None = None, session_id: str | None = None,
                index_lock=None) -> tuple[dict, Launch | None]:
    """Validate, create the new folder if any, and (when the request starts a session) remember its wrapper and
    build the launch. Returns (result for the browser, the Launch to run or None). A grouping folder is only
    created, so the dialog can walk into it; any validation or creation failure is `{ok: False, error}`.
    `index_lock` (held only while `launched.json` is written, not while git/gh run) keeps that write apart from
    an `Index.refresh` consuming the file."""
    try:
        req = parse_new(env.root, payload)
        cwd, created, warnings = req.folder, False, []
        if req.new_folder is not None:
            made = create_folder(req.folder, req.new_folder, req.kind, req.name, runner=runner,
                                 github=req.github if req.kind == "project" else None)
            cwd, created, warnings = made.path, True, list(made.warnings)
    except ValueError as e:
        return {"ok": False, "error": str(e)}, None
    except OSError as e:
        return {"ok": False, "error": f"could not create the folder: {e}"}, None
    result = {"ok": True, "path": str(cwd), "rel": rel_of(env.root, cwd), "created": created, "warnings": warnings}
    if not req.launches:
        return {**result, "launched": False}, None
    sid = session_id or str(uuid.uuid4())
    try:
        with index_lock or contextlib.nullcontext():
            remember_launch(env.data_dir, "qwen" if req.wrapper == "qwen" else "claude", sid, req.wrapper)
    except OSError as e:
        warnings.append(f"could not remember the wrapper: {e}")
    launch = new_command(cwd, req.name, req.wrapper, session_id=sid)
    warnings.extend(launch.warnings)
    return {**result, "launched": True}, launch


# ---- pending suggestions --------------------------------------------------------------------------------------------

class PendingSuggestions:
    """Model suggestions still running, by token. An entry goes once its answer has been handed out, or `ttl`
    seconds after it was added."""

    def __init__(self, ttl: float = SUGGEST_TTL_S, clock=time.monotonic):
        self._ttl = ttl
        self._clock = clock
        self._items: dict[str, tuple[float, Pending]] = {}
        self._current: Pending | None = None  # the request `supersede` issued last
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._items)

    def _purge(self) -> None:
        cutoff = self._clock() - self._ttl
        for token in [t for t, (added, _p) in self._items.items() if added <= cutoff]:
            del self._items[token]

    def add(self, pending: Pending) -> str:
        token = uuid.uuid4().hex
        with self._lock:
            self._purge()
            self._items[token] = (self._clock(), pending)
        return token

    def supersede(self, pending: Pending, idle: float | None = None) -> str:
        """Add `pending` (made with `defer_models=True`) as the dialog's current request: the one issued before it
        is cancelled (its model stages never start if they had not), and `pending`'s model stages are released
        after `idle` seconds unless a newer request supersedes it first. So typing runs the models once, for the
        name the user paused on."""
        with self._lock:
            previous, self._current = self._current, pending
        if previous is not None:
            previous.cancel()
        pending.release_after(MODEL_IDLE_S if idle is None else idle)
        return self.add(pending)

    def poll(self, token: str) -> Suggestion | None:
        """The model's answer once it is there (then the entry is dropped); None while waiting, or for an unknown or
        expired token."""
        with self._lock:
            self._purge()
            entry = self._items.get(token)
            if entry is None:
                return None
            answer = entry[1].wait(0)
            if answer is not None:
                del self._items[token]
            return answer
