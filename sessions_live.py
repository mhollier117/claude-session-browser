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
"""

from __future__ import annotations

import ctypes
import glob
import json
import os
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
        self.offset = 0
        self.custom_title = ""
        self.ai_title = ""

    def update_titles(self) -> None:
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return
        if size < self.offset:            # rewritten / truncated: start over
            self.offset, self.custom_title, self.ai_title = 0, "", ""
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
            title = self._last_entry(data, end, kind, key)
            if title:
                setattr(self, attr, title)

    @staticmethod
    def _last_entry(data: bytes, end: int, kind: str, key: str) -> str:
        marker = b'"' + kind.encode() + b'"'
        pos = end
        while True:
            idx = data.rfind(marker, 0, pos)
            if idx < 0:
                return ""
            start = data.rfind(b"\n", 0, idx) + 1
            stop = data.find(b"\n", idx)
            try:
                o = json.loads(data[start:stop if stop >= 0 else end])
                if isinstance(o, dict) and o.get("type") == kind and o.get(key):
                    return str(o[key])
            except ValueError:
                pass
            pos = start                   # not a title entry: look further back

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
        self.clock = clock
        self.proc_start = proc_start
        self._transcripts: dict[str, _Transcript] = {}

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
            t.update_titles()
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
