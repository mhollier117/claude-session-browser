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
import time
from dataclasses import dataclass
from pathlib import Path

from launcher.stores import _iso_to_epoch

TAIL_MAX_BYTES = 16 * 1024 * 1024
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


def imported_ids(home: Path) -> set[str]:
    """Thread ids Codex imported from another agent (not terminal sessions of this machine)."""
    try:
        data = json.loads((home / "external_agent_session_imports.json").read_text(encoding="utf-8"))
        records = data.get("records") if isinstance(data, dict) else None
        return {r["imported_thread_id"] for r in records or []
                if isinstance(r, dict) and isinstance(r.get("imported_thread_id"), str)}
    except (OSError, ValueError):
        return set()


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
    """id -> rollout path over `sessions/**/rollout-*.jsonl`, terminal sessions only; the newest mtime wins."""
    imported = imported_ids(home)
    found: dict[str, tuple[float, Path]] = {}
    for path in (home / "sessions").glob("**/rollout-*.jsonl"):
        meta = read_meta(path)
        if meta is None or not isinstance(meta.get("id"), str) or not is_terminal(meta, imported):
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if meta["id"] not in found or mtime > found[meta["id"]][0]:
            found[meta["id"]] = (mtime, path)
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


def _whole_lines(path: Path, size: int, n: int) -> list[bytes]:
    """The complete lines of the last `n` bytes: a mid-file read drops its partial first line, and a last line
    without its newline (still being written) is dropped."""
    start = max(0, size - n)
    with open(path, "rb") as fh:
        fh.seek(start)
        lines = fh.read().split(b"\n")
    lines = lines[:-1]  # after the final newline (empty) or a partial last line
    return lines[1:] if start > 0 else lines


def _limit_hit(rl: dict) -> bool:
    if rl.get("rate_limit_reached_type") is not None:
        return True
    for key in ("primary", "secondary"):
        win = rl.get(key)
        used = win.get("used_percent") if isinstance(win, dict) else None
        if isinstance(used, (int, float)) and used >= 100:
            return True
    return False


def _scan_tail(lines: list[bytes]) -> tuple[CodexTail, bool, bool]:
    """(tail facts of these lines, turn state seen, rate limits seen)."""
    last_ts = rate_ts = ctx_tokens = ctx_window = model = turn_model = rate_limits = None
    turn: bool | None = None
    for line in lines:
        rec = _json_obj(line)
        if rec is None:
            continue
        ts = _iso_to_epoch(rec.get("timestamp"))
        if ts is not None:
            last_ts = ts
        payload = rec.get("payload")
        if rec.get("type") == "turn_context" and isinstance(payload, dict):
            m = payload.get("model")
            if isinstance(m, str) and m.strip():
                turn_model = m.strip()
            continue
        if rec.get("type") != "event_msg" or not isinstance(payload, dict):
            continue
        kind = payload.get("type")
        if kind == "task_started":
            turn = True
        elif kind in ("task_complete", "turn_aborted"):
            turn = False
        elif kind == "token_count":
            rl = payload.get("rate_limits")
            if isinstance(rl, dict):
                rate_limits, rate_ts = rl, ts
            info = payload.get("info")
            if isinstance(info, dict):
                last = info.get("last_token_usage")
                tokens = last.get("input_tokens") if isinstance(last, dict) else None
                window = info.get("model_context_window")
                if isinstance(tokens, int):
                    ctx_tokens = tokens
                if isinstance(window, int):
                    ctx_window = window
        elif kind == "thread_settings_applied":
            settings = payload.get("thread_settings")
            m = settings.get("model") if isinstance(settings, dict) else None
            if isinstance(m, str) and m.strip():
                model = m.strip()
    tail = CodexTail(last_ts, bool(turn), _limit_hit(rate_limits) if rate_limits else False, rate_limits, rate_ts,
                     ctx_tokens, ctx_window, turn_model or model)  # turn_context is the real source
    return tail, turn is not None, rate_limits is not None


def read_tail(path: Path, tail_bytes: int = 262_144) -> CodexTail:
    """Facts from the end of a rollout, reading backwards in doubling steps (capped at 16 MB) until both a turn
    event and rate limits are found, or the start of the file."""
    try:
        size = path.stat().st_size
        n = min(max(tail_bytes, 1), TAIL_MAX_BYTES)
        while True:
            tail, has_turn, has_rate = _scan_tail(_whole_lines(path, size, n))
            if (has_turn and has_rate) or n >= size or n >= TAIL_MAX_BYTES:
                return tail
            n = min(n * 2, TAIL_MAX_BYTES)
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
    lock and no process is used twice. App-server processes (the desktop app) are ignored."""
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
    for id_, f in locks:
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        for p in pending:
            if p["create_time"] - 5 <= mtime <= p["create_time"] + 60:
                pairs.append((abs(mtime - p["create_time"]), id_, p["pid"]))
    used_pids: set[int] = set()
    for _, id_, pid in sorted(pairs):
        if id_ not in out and pid not in used_pids:
            out[id_] = pid
            used_pids.add(pid)
    return out


class CodexLive:
    """Open terminal Codex sessions as the dicts `sessions_live.LiveSessions._describe` makes, plus "tool": "codex".
    Polled about once a second, so it is cheap: the process scan, rollout listing and titles are refreshed at most
    every `enum_every` seconds (the injected clock decides), and a rollout's tail is re-read only when its
    (size, mtime) changed."""

    def __init__(self, home: Path | None = None, clock=time.time, procs=None, enum_every: float = 5.0):
        self.home = home if home is not None else codex_home()
        self.clock = clock
        self.procs = procs
        self.enum_every = enum_every
        self._enum_at: float | None = None
        self._live: dict[str, int] = {}
        self._paths: dict[str, Path] = {}
        self._titles: dict[str, str] = {}
        self._tails: dict[Path, tuple[tuple[int, float], CodexTail]] = {}
        self._metas: dict[Path, dict] = {}

    def _refresh(self, now: float) -> None:
        if self._enum_at is not None and 0 <= now - self._enum_at < self.enum_every:
            return
        self._enum_at = now
        self._live = live_threads(self.home, self.procs)
        self._paths = terminal_rollouts(self.home)
        self._titles = titles(self.home)

    def _tail(self, path: Path) -> tuple[CodexTail, float]:
        """(tail, mtime) of a rollout, the tail cached per (size, mtime)."""
        try:
            st = path.stat()
        except OSError:
            return CodexTail(None, False, False, None, None, None, None, None), 0.0
        key = (st.st_size, st.st_mtime)
        hit = self._tails.get(path)
        if hit is None or hit[0] != key:
            hit = self._tails[path] = (key, read_tail(path))
        return hit[1], st.st_mtime

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
        for path in [p for p in self._tails if p not in set(self._paths.values())]:
            self._tails.pop(path, None)
            self._metas.pop(path, None)
        return out
