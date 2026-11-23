"""Reads the OpenAI Codex CLI's own files (`~/.codex`), strictly read-only.

Rollouts are opened only with `open(..., "rb")` and bounded head/tail reads (a live one is >100 MB); the sqlite
state is opened only with `mode=ro`; `thread-writer-locks` files are never opened, only `stat`ed. Nothing is ever
written to the Codex home.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from launcher.stores import _iso_to_epoch

TAIL_MAX_BYTES = 16 * 1024 * 1024
_INCR_MAX_BYTES = 64 * 1024 * 1024  # CodexLive re-seeds instead of reading a larger appended gap
_META_MAX_BYTES = 16 * 1024 * 1024
_SIX_HOURS_MIN = 360
_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def _json_obj(line: bytes | str) -> dict | None:
    try:
        rec = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return None
    return rec if isinstance(rec, dict) else None


# Module caches, per Codex home. The service thread (CodexLive) and UI threads (usage, the index) all call in.
_CACHE_LOCK = threading.Lock()
_IMPORTS_GOOD: dict[Path, frozenset[str]] = {}       # home -> the last imports set read whole
_ROLLOUT_CACHE: dict[Path, dict[Path, dict]] = {}    # home -> rollout path -> {"id", "source"} of its first line


def _reset_caches() -> None:
    """Forget every cached read (tests)."""
    with _CACHE_LOCK:
        _IMPORTS_GOOD.clear()
        _ROLLOUT_CACHE.clear()


def imported_ids(home: Path) -> set[str]:
    """Thread ids Codex imported from another agent (not terminal sessions of this machine). A read that fails
    (the file mid-rewrite, locked, or without a `records` list) returns the last good set for this home, so the
    imported threads never flood the session list; empty when there never was one."""
    try:
        data = json.loads((home / "external_agent_session_imports.json").read_text(encoding="utf-8"))
        records = data.get("records") if isinstance(data, dict) else None
        if not isinstance(records, list):
            raise ValueError("no records list")
        ids = frozenset(r["imported_thread_id"] for r in records
                        if isinstance(r, dict) and isinstance(r.get("imported_thread_id"), str))
    except (OSError, ValueError):
        with _CACHE_LOCK:
            return set(_IMPORTS_GOOD.get(home, ()))
    with _CACHE_LOCK:
        _IMPORTS_GOOD[home] = ids
    return set(ids)


def read_meta(path: Path) -> dict | None:
    """The `payload` of the first line when it is a `session_meta` record, else None."""
    try:
        with open(path, "rb") as fh:
            buf = b""
            while b"\n" not in buf and len(buf) < _META_MAX_BYTES:
                chunk = fh.read(65_536)
                if not chunk:
                    break
                buf += chunk
    except OSError:
        return None
    rec = _json_obj(buf.split(b"\n", 1)[0])
    payload = rec.get("payload") if rec else None
    return payload if rec and rec.get("type") == "session_meta" and isinstance(payload, dict) else None


def is_terminal(meta: dict, imported: set[str]) -> bool:
    """A session run from the terminal: source "cli" (sub-agents have an object, the desktop app other strings)
    and not imported from another agent."""
    return isinstance(meta.get("source"), str) and meta["source"] == "cli" and meta.get("id") not in imported


def terminal_rollouts(home: Path) -> dict[str, Path]:
    """id -> rollout path over `sessions/**/rollout-*.jsonl`, terminal sessions only; the newest mtime wins.
    A rollout's first line never changes, so its id and source are read once per path and cached per home (paths
    that vanished are dropped); a first line that does not parse yet (Codex still writing it) is never cached. The
    imports set can change, so `is_terminal` is applied afresh on every call."""
    imported = imported_ids(home)
    with _CACHE_LOCK:
        cached = dict(_ROLLOUT_CACHE.get(home, {}))
    metas: dict[Path, dict] = {}
    found: dict[str, tuple[float, Path]] = {}
    for path in (home / "sessions").glob("**/rollout-*.jsonl"):
        meta = cached.get(path)
        if meta is None:
            full = read_meta(path)
            if full is None or not isinstance(full.get("id"), str):
                continue
            source = full.get("source")
            meta = {"id": full["id"], "source": source if isinstance(source, str) else None}
        metas[path] = meta
        if not is_terminal(meta, imported):
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if meta["id"] not in found or mtime > found[meta["id"]][0]:
            found[meta["id"]] = (mtime, path)
    with _CACHE_LOCK:
        _ROLLOUT_CACHE[home] = metas
    return {i: p for i, (_, p) in found.items()}


def _sqlite_titles(home: Path) -> dict[str, str]:
    db = home / "state_5.sqlite"
    if not db.is_file():
        return {}
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            rows = con.execute("SELECT id, title FROM threads").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return {}
    return {i: t.strip() for i, t in rows if isinstance(i, str) and isinstance(t, str) and t.strip()}


def titles(home: Path) -> dict[str, str]:
    """id -> thread title: sqlite `threads.title` (non-empty), overridden by `session_index.jsonl` `thread_name`
    (the last line per id wins)."""
    out = _sqlite_titles(home)
    try:
        with open(home / "session_index.jsonl", "rb") as fh:
            for line in fh:
                rec = _json_obj(line)
                name = rec.get("thread_name") if rec else None
                if rec and isinstance(rec.get("id"), str) and isinstance(name, str) and name.strip():
                    out[rec["id"]] = name.strip()
    except OSError:
        pass
    return out


@dataclass
class CodexTail:
    last_ts: float | None
    turn_open: bool
    limit_hit: bool  # what the latest record says, ignoring time; whether it still limits is `rate_limited`'s call
    rate_limits: dict | None
    rate_ts: float | None
    ctx_tokens: int | None
    ctx_window: int | None
    model: str | None


def _whole_lines(path: Path, size: int, n: int) -> tuple[list[bytes], int]:
    """(the complete lines of the last `n` bytes, the offset just past the last of them): a mid-file read drops its
    partial first line, and a last line without its newline (still being written) is dropped and lies past the
    offset, so an incremental read from there picks it up whole."""
    start = max(0, size - n)
    with open(path, "rb") as fh:
        fh.seek(start)
        data = fh.read()
    lines = data.split(b"\n")
    end = start + len(data) - len(lines[-1])
    lines = lines[:-1]  # after the final newline (empty) or a partial last line
    return (lines[1:] if start > 0 else lines), end


def _lines_from(path: Path, offset: int) -> tuple[list[bytes], int]:
    """(the complete lines from byte `offset` on, the offset just past the last of them)."""
    with open(path, "rb") as fh:
        fh.seek(offset)
        data = fh.read()
    cut = data.rfind(b"\n")
    return ([], offset) if cut < 0 else (data[:cut].split(b"\n"), offset + cut + 1)


def _limit_hit(rl: dict) -> bool:
    if rl.get("rate_limit_reached_type") is not None:
        return True
    for key in ("primary", "secondary"):
        win = rl.get(key)
        used = win.get("used_percent") if isinstance(win, dict) else None
        if isinstance(used, (int, float)) and used >= 100:
            return True
    return False


class _TailState:
    """The running facts of a rollout, fed its lines in file order. `read_tail` feeds a fresh one the bounded tail;
    `CodexLive` keeps one per live rollout and feeds it only the lines appended since."""

    def __init__(self) -> None:
        self.last_ts = self.rate_ts = self.ctx_tokens = self.ctx_window = None
        self.model = self.turn_model = self.rate_limits = None
        self.turn: bool | None = None  # None until a turn event is seen

    def feed(self, line: bytes) -> None:
        rec = _json_obj(line)
        if rec is None:
            return
        ts = _iso_to_epoch(rec.get("timestamp"))
        if ts is not None:
            self.last_ts = ts
        payload = rec.get("payload")
        if rec.get("type") == "turn_context" and isinstance(payload, dict):
            m = payload.get("model")
            if isinstance(m, str) and m.strip():
                self.turn_model = m.strip()
            return
        if rec.get("type") != "event_msg" or not isinstance(payload, dict):
            return
        kind = payload.get("type")
        if kind == "task_started":
            self.turn = True
        elif kind in ("task_complete", "turn_aborted"):
            self.turn = False
        elif kind == "token_count":
            rl = payload.get("rate_limits")
            if isinstance(rl, dict):
                self.rate_limits, self.rate_ts = rl, ts
            info = payload.get("info")
            if isinstance(info, dict):
                last = info.get("last_token_usage")
                tokens = last.get("input_tokens") if isinstance(last, dict) else None
                window = info.get("model_context_window")
                if isinstance(tokens, int):
                    self.ctx_tokens = tokens
                if isinstance(window, int):
                    self.ctx_window = window
        elif kind == "thread_settings_applied":
            settings = payload.get("thread_settings")
            m = settings.get("model") if isinstance(settings, dict) else None
            if isinstance(m, str) and m.strip():
                self.model = m.strip()

    def tail(self) -> CodexTail:
        rl = self.rate_limits
        return CodexTail(self.last_ts, bool(self.turn), _limit_hit(rl) if rl else False, rl, self.rate_ts,
                         self.ctx_tokens, self.ctx_window,
                         self.turn_model or self.model)  # turn_context is the real source


def _seed(path: Path, tail_bytes: int = 262_144) -> tuple[_TailState, int]:
    """(state of the end of a rollout, offset just past the last complete line read), reading backwards in doubling
    steps (capped at `TAIL_MAX_BYTES`) until both a turn event and rate limits are found, or the start of the file.
    Raises OSError."""
    size = path.stat().st_size
    n = min(max(tail_bytes, 1), TAIL_MAX_BYTES)
    while True:
        lines, end = _whole_lines(path, size, n)
        state = _TailState()
        for line in lines:
            state.feed(line)
        if (state.turn is not None and state.rate_limits is not None) or n >= size or n >= TAIL_MAX_BYTES:
            return state, end
        n = min(n * 2, TAIL_MAX_BYTES)


def read_tail(path: Path, tail_bytes: int = 262_144) -> CodexTail:
    """Facts from the end of a rollout (see `_seed`)."""
    try:
        return _seed(path, tail_bytes)[0].tail()
    except OSError:
        return CodexTail(None, False, False, None, None, None, None, None)


def _has_reset(win: dict, now: float) -> bool:
    """A rate-limit window has reset once its numeric `resets_at` is <= now; one without a `resets_at` is live."""
    resets = win.get("resets_at")
    return isinstance(resets, (int, float)) and 0 < resets <= now


def rate_limited(rl: dict | None, now: float) -> bool:
    """Whether a `rate_limits` record still limits at `now`, the one rule `usage` and `CodexLive` share:
    a live window at >= 100 %, or `rate_limit_reached_type` set while no window in the record has reset since it
    was written (once one has, the flag may name the window that reset and is ignored)."""
    if not isinstance(rl, dict):
        return False
    wins = [w for w in (rl.get("primary"), rl.get("secondary")) if isinstance(w, dict)]
    any_reset = any(_has_reset(w, now) for w in wins)
    live_full = any(not _has_reset(w, now) and isinstance(w.get("used_percent"), (int, float))
                    and w["used_percent"] >= 100 for w in wins)
    return live_full or (rl.get("rate_limit_reached_type") is not None and not any_reset)


def _window(win, now: float) -> tuple[int | None, float]:
    """(used percent, reset epoch) of one rate-limit window; a window that has reset reads 0 / 0.0."""
    if not isinstance(win, dict) or not isinstance(win.get("used_percent"), (int, float)):
        return None, 0.0
    if _has_reset(win, now):
        return 0, 0.0
    resets = win.get("resets_at")
    return int(round(win["used_percent"])), float(resets) if isinstance(resets, (int, float)) else 0.0


def usage(home: Path, now: float, recent: int = 8) -> dict | None:
    """Codex usage from the newest `rate_limits` record among the `recent` most recently modified terminal
    rollouts: {"s", "s_reset_at", "w", "w_reset_at", "limited", "plan", "at"}; None without one. A window of
    <= 360 minutes is the short ("s") one, a longer one the weekly ("w")."""
    paths = []
    for path in terminal_rollouts(home).values():
        try:
            paths.append((path.stat().st_mtime, path))
        except OSError:
            continue
    best: CodexTail | None = None
    for _, path in sorted(paths, key=lambda x: x[0], reverse=True)[:recent]:
        tail = read_tail(path)
        if tail.rate_limits is not None and tail.rate_ts is not None and (best is None or tail.rate_ts > best.rate_ts):
            best = tail
    if best is None:
        return None
    out: dict = {"s": None, "s_reset_at": 0.0, "w": None, "w_reset_at": 0.0}
    for key in ("primary", "secondary"):
        win = best.rate_limits.get(key)
        used, resets = _window(win, now)
        if used is None:
            continue
        minutes = win.get("window_minutes")
        slot = "s" if isinstance(minutes, (int, float)) and minutes <= _SIX_HOURS_MIN else "w"
        out[slot], out[slot + "_reset_at"] = used, resets
    plan = best.rate_limits.get("plan_type")
    out.update(limited=rate_limited(best.rate_limits, now), plan=plan if isinstance(plan, str) else None,
               at=best.rate_ts)
    return out


def _codex_procs() -> list[dict]:
    import psutil
    out = []
    for p in psutil.process_iter(["name"]):  # cmdline only for codex.exe: asking it of every process is slow
        try:
            if (p.info.get("name") or "").lower() == "codex.exe":
                out.append({"pid": p.pid, "create_time": p.create_time(), "cmdline": list(p.cmdline())})
        except Exception:  # noqa: BLE001 - a process vanishing or denying access is not an error here
            continue
    return out


def live_threads(home: Path, procs: list[dict] | None = None) -> dict[str, int]:
    """id -> pid of the running terminal Codex sessions. A process names its thread on its command line
    (`codex resume <id>`); one without an id (the picker, a new session) is matched to the thread-writer lock whose
    mtime lies in [create_time - 5, create_time + 60] and is closest to it, assigned greedily by smallest gap so no
    lock and no process is used twice. Processes still unmatched after that may take any unclaimed lock with
    mtime >= create_time - 5 (same greedy rule): the lock appears at the first prompt, which can come much later.
    App-server processes (the desktop app) are ignored."""
    if procs is None:
        procs = _codex_procs()
    procs = [p for p in procs if not any("app-server" in a for a in p["cmdline"])]
    if not procs:
        return {}
    known = terminal_rollouts(home)
    out: dict[str, int] = {}
    pending = []
    for p in procs:
        ids = [a for a in p["cmdline"] if _ID_RE.match(a)]
        if ids:
            if ids[-1] in known:
                out[ids[-1]] = p["pid"]
        else:
            pending.append(p)
    pairs = []
    lock_dir = home / "thread-writer-locks"
    try:
        locks = [(f.name[:-5], f) for f in lock_dir.iterdir() if f.name.endswith(".lock") and f.name[:-5] in known
                 and f.name[:-5] not in out]
    except OSError:
        locks = []
    mtimes = []
    for id_, f in locks:
        try:
            mtimes.append((id_, f.stat().st_mtime))
        except OSError:
            continue
    used_pids: set[int] = set()

    def assign(fits) -> None:
        pairs = [(abs(mtime - p["create_time"]), id_, p["pid"]) for id_, mtime in mtimes for p in pending
                 if id_ not in out and p["pid"] not in used_pids and fits(mtime, p["create_time"])]
        for _, id_, pid in sorted(pairs):
            if id_ not in out and pid not in used_pids:
                out[id_] = pid
                used_pids.add(pid)

    assign(lambda mtime, ct: ct - 5 <= mtime <= ct + 60)
    # Codex makes the rollout and its lock at the first prompt, not at start: a process still unmatched (its first
    # prompt came later than 60 s) may take any unclaimed lock made after it started. Strictly after the window
    # pass, so a match within 60 s always wins.
    assign(lambda mtime, ct: mtime >= ct - 5)
    return out


class CodexLive:
    """Open terminal Codex sessions as the dicts `sessions_live.LiveSessions._describe` makes, plus "tool": "codex".
    Polled about once a second, so it is cheap: the process scan, rollout listing and titles are refreshed at most
    every `enum_every` seconds (the injected clock decides). A live rollout's state is seeded once with the bounded
    tail read (`_seed`) and then fed only the complete lines appended since, so a long turn stays open however far
    back its task_started lies. The file shrinking (rewritten) or a gap over `_INCR_MAX_BYTES` re-seeds; a rollout
    that stops being live drops its state."""

    def __init__(self, home: Path | None = None, clock=time.time, procs=None, enum_every: float = 5.0):
        self.home = home if home is not None else codex_home()
        self.clock = clock
        self.procs = procs
        self.enum_every = enum_every
        self._enum_at: float | None = None
        self._live: dict[str, int] = {}
        self._paths: dict[str, Path] = {}
        self._titles: dict[str, str] = {}
        self._tails: dict[Path, list] = {}  # path -> [_TailState, offset just past the last line fed]
        self._metas: dict[Path, dict] = {}

    def _refresh(self, now: float) -> None:
        if self._enum_at is not None and 0 <= now - self._enum_at < self.enum_every:
            return
        self._enum_at = now
        self._live = live_threads(self.home, self.procs)
        self._paths = terminal_rollouts(self.home)
        self._titles = titles(self.home)

    def _tail(self, path: Path) -> tuple[CodexTail, float]:
        """(tail, mtime) of a rollout: seeded on first sight, after a shrink or a too-large gap; otherwise only the
        complete lines appended since the last poll are read."""
        try:
            st = path.stat()
            entry = self._tails.get(path)
            if entry is None or st.st_size < entry[1] or st.st_size - entry[1] > _INCR_MAX_BYTES:
                entry = self._tails[path] = list(_seed(path))
            elif st.st_size > entry[1]:
                lines, entry[1] = _lines_from(path, entry[1])
                for line in lines:
                    entry[0].feed(line)
        except OSError:
            self._tails.pop(path, None)
            return CodexTail(None, False, False, None, None, None, None, None), 0.0
        return entry[0].tail(), st.st_mtime

    def snapshot(self) -> list[dict]:
        import sessions_live as sl
        now = self.clock()
        self._refresh(now)
        out = []
        for id_, pid in self._live.items():
            path = self._paths.get(id_)
            if path is None:
                continue
            tail, mtime = self._tail(path)
            meta = self._metas.get(path)
            if meta is None:
                meta = self._metas[path] = read_meta(path) or {}
            since = tail.last_ts or mtime
            if rate_limited(tail.rate_limits, now):
                state = sl.LIMIT
            elif tail.turn_open:
                state = sl.WORKING
            else:
                age = now - since if since else 1e9
                state = sl.DONE if age < sl.DONE_FOR_S else (sl.IDLE if age < sl.SLEEP_AFTER_S else sl.SLEEP)
            cwd = str(meta.get("cwd") or "")
            folder = os.path.basename(cwd.rstrip("\\/")) or cwd
            title = self._titles.get(id_, "")
            ctx_pct = (sl.context_pct(tail.ctx_tokens, tail.ctx_window)
                       if tail.ctx_tokens is not None and tail.ctx_window else None)
            out.append({
                "session_id": id_, "pid": pid, "cwd": cwd, "folder": folder,
                "name": (title or folder)[:sl.NAME_MAX], "named": bool(title),
                "status": "busy" if state in (sl.WORKING, sl.THINKING) else "idle",
                "waiting_for": "", "state": state, "since": since,
                "started": _iso_to_epoch(meta.get("timestamp")) or 0.0,
                "ctx_tokens": tail.ctx_tokens, "ctx_window": tail.ctx_window if ctx_pct is not None else None,
                "ctx_pct": ctx_pct, "tool": "codex",
            })
        live_paths = {self._paths.get(i) for i in self._live}
        for path in [p for p in self._tails if p not in live_paths]:
            self._tails.pop(path, None)
        for path in [p for p in self._metas if p not in live_paths]:
            self._metas.pop(path, None)
        return out
