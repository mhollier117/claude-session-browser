"""The session index: one sqlite row per Claude/Qwen transcript, kept current by cheap refreshes.

A refresh lists every transcript, re-reads only files whose (size, mtime) changed, and deletes rows whose file
is gone. Facts come from bounded head/tail reads (`read_*(count=False)`); message counts and titles come from
`count_messages`, which on a file that only grew starts at the stored `counted_to` offset, so a large live
transcript is never rescanned whole. Liveness and display names are worked out when rows are read, not stored.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sessions_live import LiveSessions

from . import stores
from .paths import Env

_LAST_REPLY_CHARS = 400
_FIRST_ASK_NAME_CHARS = 60
_UNTITLED = "(untitled)"

_SCHEMA_VERSION = 1  # bump when the columns change: an older cache is dropped and rebuilt
_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    store TEXT NOT NULL, sid TEXT NOT NULL, path TEXT NOT NULL, cwd TEXT, name TEXT, display_name TEXT,
    first_ask TEXT, last_ask TEXT, last_reply TEXT, user_msgs INTEGER, assistant_msgs INTEGER,
    size INTEGER, mtime REAL, last_activity REAL, wrapper TEXT,
    counted_to INTEGER, custom_title TEXT, ai_title TEXT,
    PRIMARY KEY (store, sid))
"""
_FACT_COLS = ("path", "cwd", "name", "display_name", "first_ask", "last_ask", "last_reply", "user_msgs",
              "assistant_msgs", "size", "mtime", "last_activity", "counted_to", "custom_title", "ai_title")


@dataclass
class Row:
    store: str
    sid: str
    cwd: str | None
    name: str | None
    display_name: str
    first_ask: str
    last_ask: str
    last_reply: str
    user_msgs: int
    assistant_msgs: int
    size: int
    mtime: float
    last_activity: float
    live_pid: int | None
    wrapper: str | None
    cwd_exists: bool
    path: str = ""  # the transcript file (the preview reads the whole last reply from it)


@dataclass
class FolderStats:
    sessions: int
    last_activity: float | None
    names: list[str]


@dataclass
class _Live:
    pid: int
    name: str
    named: bool


def claude_folder_name(path: Path) -> str:
    """Claude Code's project folder name for a cwd: every character outside [A-Za-z0-9] becomes '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def _default_pid_exists(pid: int) -> bool:
    import psutil
    return psutil.pid_exists(pid)


def _read_json(path: Path) -> dict:
    """The JSON object in `path`, or {} when it is missing, mid-write or not an object."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _str(value) -> str:
    return value if isinstance(value, str) else ""


def _parts(path: str | Path) -> tuple[str, ...]:
    return tuple(p.lower() for p in Path(path).parts)


class Index:
    def __init__(self, env: Env, db_path: Path | None = None,
                 claude_live: Callable[[], list[dict] | None] | None = None,
                 pid_exists: Callable[[int], bool] | None = None):
        """`claude_live` (a `LiveSessions.snapshot`-shaped callable) and `pid_exists` (Qwen registry check) are
        injectable for tests; by default the real Claude registry and `psutil.pid_exists` are used."""
        self.env = env
        self.db_path = Path(db_path) if db_path else env.data_dir / "index.sqlite"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # one LiveSessions for the Index's lifetime: it reads each live transcript incrementally after the first pass
        self._claude_live = claude_live or LiveSessions(claude_home=str(env.claude_home)).snapshot
        self._pid_exists = pid_exists or _default_pid_exists
        self._db = sqlite3.connect(self.db_path)
        self._db.row_factory = sqlite3.Row
        with self._db:
            if self._db.execute("PRAGMA user_version").fetchone()[0] != _SCHEMA_VERSION:
                self._db.execute("DROP TABLE IF EXISTS sessions")  # only a cache: rebuilt by the next refresh
                self._db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            self._db.execute(_SCHEMA)

    # ---- refresh -----------------------------------------------------------------------------------------------

    def _transcripts(self) -> dict[tuple[str, str], tuple[Path, os.stat_result]]:
        """(store, sid) -> (path, stat) of every transcript; when one sid sits in several project folders, the
        newest file wins, so the row does not flip between them from one refresh to the next."""
        skip = claude_folder_name(self.env.data_dir / "suggest-cwd").lower()
        found: dict[tuple[str, str], tuple[Path, os.stat_result]] = {}
        for store, home, pattern in (("claude", self.env.claude_home, "*.jsonl"),
                                     ("qwen", self.env.qwen_home, "chats/*.jsonl")):
            projects = home / "projects"
            if not projects.is_dir():
                continue
            for folder in sorted(projects.iterdir()):
                if not folder.is_dir() or folder.name.lower() == skip:
                    continue
                for path in sorted(folder.glob(pattern)):
                    if store == "claude" and not stores.is_top_level_claude(path):
                        continue
                    try:
                        st = path.stat()
                    except OSError:
                        continue  # vanished mid-listing
                    key = (store, path.stem)
                    if key not in found or st.st_mtime > found[key][1].st_mtime:
                        found[key] = (path, st)
        return found

    def refresh(self, progress: Callable[[int, int], None] | None = None) -> int:
        """Bring the index in line with the transcripts on disk; returns rows inserted, updated or deleted."""
        files = self._transcripts()
        live = self._live()
        titles = self._titles()
        touched = 0
        with self._db:  # one transaction: an unexpected error rolls the whole refresh back
            old = {(r["store"], r["sid"]): r for r in self._db.execute(
                "SELECT store, sid, path, size, mtime, counted_to, user_msgs, assistant_msgs, custom_title, ai_title "
                "FROM sessions")}
            for done, (key, (path, st)) in enumerate(files.items(), 1):
                prev = old.get(key)
                if not (prev and prev["path"] == str(path) and prev["size"] == st.st_size
                        and prev["mtime"] == st.st_mtime):
                    try:
                        self._index_file(key[0], path, prev, live, titles)
                        touched += 1
                    except OSError:
                        pass  # vanished or locked mid-refresh: the next refresh sees it again
                if progress:
                    progress(done, len(files))
            for key in old.keys() - files.keys():
                self._db.execute("DELETE FROM sessions WHERE store = ? AND sid = ?", key)
                touched += 1
        return touched

    def _index_file(self, store: str, path: Path, prev, live, titles) -> None:
        facts = getattr(stores, f"read_{store}")(path, count=False)
        grown = (prev is not None and prev["path"] == str(path) and prev["counted_to"] is not None
                 and facts.size >= prev["size"] and facts.size >= prev["counted_to"])
        if grown:
            users, assistants, end, custom, ai = stores.count_messages(path, store, prev["counted_to"])
            users += prev["user_msgs"]
            assistants += prev["assistant_msgs"]
            custom = custom or prev["custom_title"]
            ai = ai or prev["ai_title"]
        else:
            users, assistants, end, custom, ai = stores.count_messages(path, store)
        row = {"path": str(path), "cwd": facts.cwd, "name": custom or ai, "first_ask": facts.first_ask,
               "last_ask": facts.last_ask, "last_reply": facts.last_reply[:_LAST_REPLY_CHARS],
               "user_msgs": users, "assistant_msgs": assistants, "size": facts.size, "mtime": facts.mtime,
               "last_activity": facts.mtime, "counted_to": end, "custom_title": custom, "ai_title": ai}
        row["display_name"] = self._display_name(store, facts.sid, row, live, titles)
        cols = ", ".join(_FACT_COLS)
        self._db.execute(
            f"INSERT INTO sessions (store, sid, {cols}) VALUES ({', '.join('?' * (len(_FACT_COLS) + 2))}) "
            f"ON CONFLICT(store, sid) DO UPDATE SET {', '.join(f'{c} = excluded.{c}' for c in _FACT_COLS)}",
            (store, facts.sid, *(row[c] for c in _FACT_COLS)))

    # ---- names and liveness ------------------------------------------------------------------------------------

    def _titles(self) -> dict[str, str]:
        try:
            data = json.loads((self.env.claude_home / "session_titles.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {k: v for k, v in data.items() if isinstance(v, str) and v} if isinstance(data, dict) else {}

    def _live(self) -> dict[tuple[str, str], _Live]:
        """Live sessions with their raw registry `name`. The snapshot's own `name` is not used: it falls back to
        the folder name, which would hide the first ask and change when the process exits."""
        out: dict[tuple[str, str], _Live] = {}
        for s in self._claude_live() or []:
            pid = s.get("pid")
            if isinstance(pid, int) and s.get("session_id"):
                rec = _read_json(self.env.claude_home / "sessions" / f"{pid}.json")
                out[("claude", s["session_id"])] = _Live(pid, _str(rec.get("name")), bool(s.get("named")))
        for entry in sorted((self.env.qwen_home / "sessions").glob("*.json")):
            rec = _read_json(entry)
            pid, sid = rec.get("pid"), rec.get("sessionId")
            if isinstance(pid, int) and not isinstance(pid, bool) and isinstance(sid, str) and sid \
                    and self._pid_exists(pid):
                out[("qwen", sid)] = _Live(pid, _str(rec.get("name")), False)
        return out

    @staticmethod
    def _display_name(store: str, sid: str, row, live: dict, titles: dict) -> str:
        reg = live.get((store, sid))
        first = (row["first_ask"] or "")[:_FIRST_ASK_NAME_CHARS]
        for candidate in (titles.get(sid), row["custom_title"], reg.name if reg and reg.named else None,
                          row["ai_title"], reg.name if reg else None, first):
            if candidate:
                return candidate
        return _UNTITLED

    # ---- reading -----------------------------------------------------------------------------------------------

    def _to_rows(self, records, live=None, titles=None) -> list[Row]:
        live = self._live() if live is None else live
        titles = self._titles() if titles is None else titles
        out = []
        for r in records:
            reg = live.get((r["store"], r["sid"]))
            out.append(Row(
                store=r["store"], sid=r["sid"], cwd=r["cwd"], name=r["name"],
                display_name=self._display_name(r["store"], r["sid"], r, live, titles),
                first_ask=r["first_ask"] or "", last_ask=r["last_ask"] or "", last_reply=r["last_reply"] or "",
                user_msgs=r["user_msgs"], assistant_msgs=r["assistant_msgs"], size=r["size"], mtime=r["mtime"],
                last_activity=r["last_activity"], live_pid=reg.pid if reg else None, wrapper=r["wrapper"],
                cwd_exists=bool(r["cwd"]) and Path(r["cwd"]).is_dir(), path=r["path"]))
        return out

    def rows(self, filter_text: str = "") -> list[Row]:
        """Every session, newest activity first; within the same minute live sessions come first.
        `filter_text` matches (case-insensitively) the display name, transcript name, cwd and first ask."""
        rows = self._to_rows(self._db.execute("SELECT * FROM sessions").fetchall())
        needle = filter_text.strip().lower()
        if needle:
            rows = [r for r in rows
                    if any(needle in (v or "").lower() for v in (r.display_name, r.name, r.cwd, r.first_ask))]
        rows.sort(key=lambda r: (-int(r.last_activity // 60), r.live_pid is None, -r.last_activity))
        return rows

    def row(self, store: str, sid: str) -> Row | None:
        rec = self._db.execute("SELECT * FROM sessions WHERE store = ? AND sid = ?", (store, sid)).fetchone()
        return self._to_rows([rec])[0] if rec else None

    def record_launch(self, store: str, sid: str, wrapper: str) -> None:
        """Remember which wrapper last launched this session (kept across refreshes)."""
        self._db.execute("UPDATE sessions SET wrapper = ? WHERE store = ? AND sid = ?", (wrapper, store, sid))
        self._db.commit()

    def folder_stats(self, rel_path: str) -> FolderStats:
        """Sessions whose cwd is `env.root / rel_path` or below it (case-insensitive, whole path components)."""
        want = _parts(self.env.root / rel_path)
        hits = sorted((r for r in self.rows() if r.cwd and _parts(r.cwd)[:len(want)] == want),
                      key=lambda r: -r.last_activity)
        return FolderStats(sessions=len(hits), last_activity=max((r.last_activity for r in hits), default=None),
                           names=[r.display_name for r in hits[:3]])

    def close(self) -> None:
        self._db.close()
