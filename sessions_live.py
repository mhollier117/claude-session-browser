"""Which Claude Code sessions are open, what each is doing, and its name.

Reads Claude Code's own live session registry instead of guessing from
transcripts and window titles. Claude Code (2.1.x) writes one file per running
process, ``~/.claude/sessions/<pid>.json``, with ``sessionId``, ``cwd``,
``kind``, ``procStart`` (process creation FILETIME), ``name``/``nameSource``,
``status`` (busy | shell | idle | waiting), ``waitingFor`` and
``statusUpdatedAt`` (ms). Verified on 2.1.316:

* ``waiting``/"input needed" while a question dialog or permission prompt is
  open, back to ``busy`` once answered.
* ``shell`` while a background shell command is still running.
* ``busy`` while Claude works, or while background tasks/agents run.
* ``idle`` when a reply is finished (also one that ends in a plain question).

Nothing here writes to Claude Code's files; the ``<pid>.<hash>.key`` files next
to them are never opened. No hooks, no setup, no cost to Claude.

Session names come from the session's transcript: the last ``custom-title``
(the user's ``/rename`` or ``claude -n``) wins over the last ``ai-title``
(Claude's automatic title), then the registry name, then the folder name.

Context-window usage (for the Clawdmeter donut, "33% of 1M") comes from the
same transcript, see ``_Transcript._update_context`` and ``context_window``.
"""

from __future__ import annotations

import ctypes
import glob
import json
import os
import re
import sys
import time

# Session states, most urgent first.
NEEDS_YOU, LIMIT, THINKING, WORKING, DONE, IDLE, SLEEP = (
    "needs_you", "limit", "thinking", "working", "done", "idle", "sleep")
URGENCY = {NEEDS_YOU: 0, LIMIT: 1, THINKING: 2, WORKING: 2, DONE: 3, IDLE: 4, SLEEP: 5}

DONE_FOR_S = 180          # "just finished" for 3 minutes after a reply ends
SLEEP_AFTER_S = 900       # idle for 15 minutes -> sleep
TAIL_BYTES = 16 * 1024    # how much of a transcript's end decides thinking/limit
NAME_MAX = 40
WINDOW_200K, WINDOW_1M = 200_000, 1_000_000


def _claude_home() -> str:
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


# --------------------------------------------------------------------------
# Is a process still the one that wrote the registry file?
# --------------------------------------------------------------------------

def proc_start_filetime(pid: int) -> int | None:
    """Creation time of `pid` as a Windows FILETIME, or None if not running.
    Comparing it with the registry's ``procStart`` makes a reused PID harmless."""
    if sys.platform != "win32":
        return None
    from ctypes import wintypes as W
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = W.HANDLE
    h = k.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        c, e, kk, u = W.FILETIME(), W.FILETIME(), W.FILETIME(), W.FILETIME()
        if not k.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e),
                                 ctypes.byref(kk), ctypes.byref(u)):
            return None
        return (c.dwHighDateTime << 32) | c.dwLowDateTime
    finally:
        k.CloseHandle(h)


# --------------------------------------------------------------------------
# Transcript reading (names, thinking, limit)
# --------------------------------------------------------------------------

class _Transcript:
    """Incremental reader for one session's transcript: remembers how far it
    has read, so after the first pass only new lines are looked at."""

    def __init__(self, path: str):
        self.path = path
        self._reset()

    def _reset(self) -> None:
        self.offset = 0
        self.custom_title = ""
        self.ai_title = ""
        # Context usage of the main conversation, from the last usage entry:
        self.ctx_tokens = None            # input + cache read + cache creation
        self.model = ""                   # requestedModel (keeps "[1m]"), else message.model
        self.beta_1m = None               # last main api-request asked for context-1m
        self.ctx_peak = 0                 # highest ctx_tokens seen since the model last changed

    def update(self) -> None:
        """Reads what was appended since last time: titles and context usage."""
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return
        if size < self.offset:            # rewritten / truncated: start over
            self._reset()
        if size == self.offset:
            return
        try:
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                data = f.read(size - self.offset)
        except OSError:
            return
        end = data.rfind(b"\n")
        if end < 0:
            return                        # no complete line yet
        self.offset += end + 1
        # Jump straight to the LAST title entry of each kind (byte search runs
        # at C speed; walking every line of a 100 MB transcript took seconds).
        # The bare '"ai-title"' can't match inside message text, where the
        # quotes are escaped (\"ai-title\"); the parsed type is checked anyway.
        for kind, key, attr in (("custom-title", "customTitle", "custom_title"),
                                ("ai-title", "aiTitle", "ai_title")):
            o = self._last_line(data, end, b'"' + kind.encode() + b'"',
                                lambda o, kind=kind, key=key: o.get("type") == kind and o.get(key))
            if o:
                setattr(self, attr, str(o[key]))
        self._update_context(data, end)

    def _update_context(self, data: bytes, end: int) -> None:
        """Context usage the way Claude Code's status line counts it: the LAST
        entry with ``message.usage``, skipping sidechain and API-error entries;
        tokens = input + cache_read_input + cache_creation_input. Same backward
        byte search as the titles, so a 100 MB transcript is not walked line by
        line, and later calls only see the newly appended bytes."""
        req = self._last_line(data, end, b'"api-request"', _is_main_api_request)
        if req:
            betas = (req.get("params") or {}).get("betas") or []
            self.beta_1m = any("context-1m" in str(b) for b in betas)
        o = self._last_line(data, end, b'"usage"', _is_usage_entry)
        if not o:
            return
        msg = o["message"]
        u = msg["usage"]
        tokens = sum(int(u.get(k) or 0) for k in (
            "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        model = str(o.get("requestedModel") or msg.get("model") or "")
        if model != self.model:
            self.model, self.ctx_peak = model, 0
        self.ctx_tokens = tokens
        self.ctx_peak = max(self.ctx_peak, tokens)

    @staticmethod
    def _last_line(data: bytes, end: int, marker: bytes, accept) -> dict | None:
        """The last complete line containing `marker` whose JSON object passes
        `accept`, or None. The bare marker can't match inside message text,
        where the quotes are escaped; `accept` checks the parsed entry anyway."""
        pos = end
        while True:
            idx = data.rfind(marker, 0, pos)
            if idx < 0:
                return None
            start = data.rfind(b"\n", 0, idx) + 1
            stop = data.find(b"\n", idx)
            try:
                o = json.loads(data[start:stop if stop >= 0 else end])
                if isinstance(o, dict) and accept(o):
                    return o
            except ValueError:
                pass
            pos = start                   # not the entry we want: look further back

    def tail_entries(self) -> list[dict]:
        """The last few complete JSON lines, newest last."""
        try:
            size = os.path.getsize(self.path)
            with open(self.path, "rb") as f:
                f.seek(max(0, size - TAIL_BYTES))
                data = f.read()
        except OSError:
            return []
        lines = data.split(b"\n")
        if size > TAIL_BYTES:
            lines = lines[1:]             # first line is probably cut
        out = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out


def _is_usage_entry(o: dict) -> bool:
    if o.get("type") != "assistant" or o.get("isSidechain") or o.get("isApiErrorMessage"):
        return False
    msg = o.get("message")
    if not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
        return False
    # Claude Code's own placeholder replies carry an all-zero usage.
    return msg.get("model") != "<synthetic>"


def _is_main_api_request(o: dict) -> bool:
    return o.get("type") == "api-request" and o.get("querySource") in (
        None, "repl_main_thread", "sdk")


# --------------------------------------------------------------------------
# Context window size
# --------------------------------------------------------------------------
#
# Claude Code hands ``context_window.context_window_size`` only to the status
# line, never to a file. What the transcripts show (checked read-only on ~700
# transcripts from Claude Code 2.1.1xx-2.1.316, Nov 2026):
#
# * Assistant entries carry ``requestedModel`` (2.1.3xx), which keeps the
#   "[1m]" suffix when the user picked a 1M variant ("claude-opus-5-5[1m]");
#   ``message.model`` never has it. ``api-request`` entries list the API betas
#   in ``params.betas``; "context-1m-..." shows up for those variants (and for
#   a few plain-id sessions). An ``attachment.identity`` entry repeats the id,
#   with "(1M context)" in its ``marketingName``.
# * Plain ids of the current models run past 200K without either signal:
#   claude-opus-5-5, claude-opus-5, claude-opus-4-8, claude-fable-5-1 and
#   claude-fable-5 all have sessions at 400K-970K, and auto-compaction fired
#   at 960K-990K. The status line reports 1,000,000 for a plain
#   claude-opus-5-5. So those families are 1M natively; claude-opus-4-6 only
#   went past 200K as "[1m]", haiku is 200K.
# * Neither the session registry, settings.json nor ~/.claude.json records
#   which model a running session uses.
#
# Rule, first match wins:
#   1. "[1m]" in the session's model id, or its last main API request used
#      the context-1m beta                                            -> 1M
#   2. the session already used more than 200K since its model changed -> 1M
#   3. model id is claude-opus / claude-fable, version 4.8 or newer    -> 1M
#   4. no model id seen and settings.json "model" contains "[1m]"      -> 1M
#   5. otherwise                                                       -> 200K

_FAMILY = re.compile(r"claude-(opus|fable|sonnet|haiku)-(\d+)(?:-(\d{1,2}))?(?!\d)")


def context_window(model: str, beta_1m: bool | None = None, peak_tokens: int = 0,
                   default_model: str = "") -> int:
    """Size of a session's context window in tokens (rule above)."""
    model = (model or "").lower()
    if "[1m]" in model or beta_1m:
        return WINDOW_1M
    if peak_tokens > WINDOW_200K:
        return WINDOW_1M
    m = _FAMILY.search(model)
    if m and m.group(1) in ("opus", "fable") and (int(m.group(2)), int(m.group(3) or 0)) >= (4, 8):
        return WINDOW_1M
    if not model and "[1m]" in (default_model or "").lower():
        return WINDOW_1M
    return WINDOW_200K


def window_label(size: int) -> str:
    """1_000_000 -> "1M", 200_000 -> "200K"."""
    if size >= 1_000_000:
        return f"{size // 1_000_000}M"
    return f"{size // 1000}K"


def context_pct(tokens: int, size: int) -> int:
    """Whole percent of the window in use, 0-100."""
    return max(0, min(100, int(round(tokens * 100 / size)))) if size else 0


def _is_limit_entry(o: dict) -> bool:
    """Claude Code's usage-limit message: an assistant entry flagged as an API
    error with HTTP 429 / error "rate_limit"."""
    if not isinstance(o, dict) or o.get("type") != "assistant":
        return False
    if not (o.get("isApiErrorMessage") or o.get("isApiError")):
        return False
    err = o.get("error")
    return o.get("apiErrorStatus") == 429 or err == "rate_limit" or (
        isinstance(err, dict) and err.get("type") == "rate_limit_error")


def _last_block_type(entries: list[dict]) -> str:
    for o in reversed(entries):
        if o.get("type") != "assistant":
            continue
        content = (o.get("message") or {}).get("content")
        if isinstance(content, list) and content:
            last = content[-1]
            if isinstance(last, dict):
                return str(last.get("type") or "")
        return ""
    return ""


# --------------------------------------------------------------------------
# The snapshot
# --------------------------------------------------------------------------

class LiveSessions:
    """``snapshot()`` returns every open interactive session, most urgent first,
    or None when this Claude Code keeps no session registry (older versions):
    callers then fall back to their old detection."""

    def __init__(self, claude_home: str | None = None, clock=time.time,
                 proc_start=proc_start_filetime):
        home = claude_home or _claude_home()
        self.sessions_dir = os.path.join(home, "sessions")
        self.projects_dir = os.path.join(home, "projects")
        self.settings_path = os.path.join(home, "settings.json")
        self.clock = clock
        self.proc_start = proc_start
        self._transcripts: dict[str, _Transcript] = {}
        self._settings_cache = (None, "")     # (mtime, its "model")

    def _default_model(self) -> str:
        """The "model" in Claude Code's settings.json (re-read only when the
        file changes)."""
        try:
            mtime = os.path.getmtime(self.settings_path)
        except OSError:
            return ""
        if self._settings_cache[0] != mtime:
            model = ""
            try:
                with open(self.settings_path, encoding="utf-8") as f:
                    model = str((json.load(f) or {}).get("model") or "")
            except (OSError, ValueError, AttributeError):
                pass
            self._settings_cache = (mtime, model)
        return self._settings_cache[1]

    def _transcript(self, session_id: str) -> _Transcript | None:
        t = self._transcripts.get(session_id)
        if t is None:
            hits = glob.glob(os.path.join(self.projects_dir, "*", session_id + ".jsonl"))
            if not hits:
                return None
            t = self._transcripts[session_id] = _Transcript(hits[0])
        return t

    def _alive(self, rec: dict) -> bool:
        pid = rec.get("pid")
        if not isinstance(pid, int):
            return False
        started = self.proc_start(pid)
        if started is None:
            return False
        want = rec.get("procStart")
        return want is None or str(want) == str(started)

    def snapshot(self) -> list[dict] | None:
        if not os.path.isdir(self.sessions_dir):
            return None
        now = self.clock()
        out = []
        seen = set()
        for entry in os.scandir(self.sessions_dir):
            name = entry.name
            if not (name.endswith(".json") and name[:-5].isdigit()):
                continue                  # skips the .key files
            try:
                with open(entry.path, encoding="utf-8") as f:
                    rec = json.load(f)
            except (OSError, ValueError):
                continue
            if rec.get("kind", "interactive") != "interactive" or not self._alive(rec):
                continue
            sid = str(rec.get("sessionId") or "")
            if not sid or sid in seen:
                continue
            seen.add(sid)
            out.append(self._describe(rec, sid, now))
        # Forget transcripts of sessions that are gone.
        for sid in list(self._transcripts):
            if sid not in seen:
                del self._transcripts[sid]
        out.sort(key=lambda s: (URGENCY[s["state"]], -s["since"]))
        return out

    def _describe(self, rec: dict, sid: str, now: float) -> dict:
        status = str(rec.get("status") or "idle")
        since = float(rec.get("statusUpdatedAt") or rec.get("updatedAt") or 0) / 1000.0
        t = self._transcript(sid)
        if t:
            t.update()
        tail = t.tail_entries() if t and status in ("busy", "idle") else []

        if status == "waiting":
            state = NEEDS_YOU
        elif status == "idle" and tail and _is_limit_entry(tail[-1]):
            state = LIMIT
        elif status in ("busy", "shell"):
            state = THINKING if (status == "busy" and _last_block_type(tail) == "thinking") else WORKING
        else:
            age = now - since if since else 1e9
            state = DONE if age < DONE_FOR_S else (IDLE if age < SLEEP_AFTER_S else SLEEP)

        cwd = str(rec.get("cwd") or "")
        folder = os.path.basename(cwd.rstrip("\\/")) or cwd
        custom = (t.custom_title if t else "") or (
            str(rec.get("name") or "") if rec.get("nameSource") == "user" else "")
        name = custom or (t.ai_title if t else "") or str(rec.get("name") or "") or folder
        ctx_pct = ctx_window = None           # unknown until the first reply
        if t and t.ctx_tokens is not None:
            ctx_window = context_window(t.model, t.beta_1m, t.ctx_peak,
                                        "" if t.model else self._default_model())
            ctx_pct = context_pct(t.ctx_tokens, ctx_window)
        return {
            "session_id": sid,
            "pid": rec.get("pid"),
            "cwd": cwd,
            "folder": folder,
            "name": name[:NAME_MAX],
            "named": bool(custom),          # deliberately named by the user
            "status": status,
            "waiting_for": rec.get("waitingFor") or "",
            "state": state,
            "since": since,
            "started": float(rec.get("startedAt") or 0) / 1000.0,
            "ctx_tokens": t.ctx_tokens if t else None,
            "ctx_window": ctx_window,         # tokens, None = unknown
            "ctx_pct": ctx_pct,               # 0-100, None = unknown
        }


class SessionsService:
    """Takes a snapshot about once a second in its own thread, so readers (the
    desktop Buddy's animation loop, the Clawdmeter link) never wait on disk.
    ``latest()`` is None until the first snapshot or without a registry."""

    INTERVAL = 1.0

    def __init__(self, live: LiveSessions | None = None, log=None):
        import threading
        self.live = live or LiveSessions()
        self._log = log or (lambda msg: None)
        self._latest = None
        self._stop = threading.Event()
        self._thread = None

    def start(self) -> None:
        import threading
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="sessions")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def latest(self):
        return self._latest

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._latest = self.live.snapshot()
            except Exception as e:          # keep running; old detection takes over
                self._latest = None
                self._log(f"sessions: snapshot failed: {e}")
            self._stop.wait(self.INTERVAL)


WAIT_ARM_S = 5.0          # a prompt must stay open this long to count as waiting


def gadget_view(sessions: list[dict], now: float, form=None, name_max: int = NAME_MAX,
                limit: int = 8) -> list[dict]:
    """The session list as the Clawdmeter should see it (its auto-rotation).

    Each entry is a copy of the snapshot entry with three extra keys:

    * ``gstate``: the state the gadget shows. A prompt open for less than
      WAIT_ARM_S counts as WORKING, so approvals answered at the keyboard
      within a few seconds never take over the gadget. The end of a wait is
      immediate (the registry is already back to busy).
    * ``wait_since``: epoch seconds the wait began, only for counted waits.
      Constant for the whole wait, so it costs one resend per wait; the gadget
      works out the age itself.
    * ``gname``: the display name, ``form(name)`` (e.g. ASCII, cut to
      ``name_max``), made unique within the list ("api~2"), so the gadget's
      name-keyed rotation, pop-up and focus requests can't mix sessions up.

    Order: counted waits first, longest-waiting first; then by gadget state
    urgency; inside a state by when the session started. Not by the newest
    status change (the snapshot's order), which reshuffles the list, and so
    every message index, whenever a session flips between busy and shell.
    At most ``limit`` entries; the least urgent are dropped."""
    form = form or (lambda n: n)
    view = []
    for s in sessions:
        e = dict(s)
        waiting = s["state"] == NEEDS_YOU
        armed = waiting and bool(s.get("since")) and now - s["since"] >= WAIT_ARM_S
        e["gstate"] = WORKING if waiting and not armed else s["state"]
        e["wait_since"] = s["since"] if armed else None
        view.append(e)
    view.sort(key=lambda e: (
        0 if e["wait_since"] is not None else 1,
        e["wait_since"] or 0.0,
        URGENCY[e["gstate"]],
        e.get("started") or 0.0,
        str(e.get("session_id") or "")))
    view = view[:limit]
    taken = set()
    for e in view:
        base = str(form(e.get("name") or "")).rstrip()
        name, n = base, 1
        while name in taken:
            n += 1
            suffix = f"~{n}"
            name = base[:max(0, name_max - len(suffix))].rstrip() + suffix
        taken.add(name)
        e["gname"] = name
    return view


def hint(sessions: list[dict]) -> str:
    """Short note about the OTHER open sessions, e.g. "+2 working"."""
    others = sessions[1:]
    if not others:
        return ""
    need = sum(1 for s in others if s["state"] == NEEDS_YOU)
    if need:
        return f"+{need} need{'s' if need == 1 else ''} you"
    busy = sum(1 for s in others if s["state"] in (THINKING, WORKING))
    if busy:
        return f"+{busy} working"
    return f"+{len(others)} open"


if __name__ == "__main__":
    snap = LiveSessions().snapshot()
    if snap is None:
        print("No session registry (Claude Code too old?)")
    for s in snap or []:
        print(f"{s['state']:<9} {s['status']:<7} {'*' if s['named'] else ' '} {s['name']:<40} "
              f"{s['folder']:<28} {time.strftime('%H:%M:%S', time.localtime(s['since']))}")
    if snap:
        print("hint for the top session:", hint(snap) or "(none)")
