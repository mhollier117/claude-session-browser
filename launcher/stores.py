"""Transcript readers: Claude Code and Qwen Code session files reduced to the facts the launcher shows.

Claude record keys relied on: top-level `type` (user|assistant|ai-title|custom-title), `cwd`,
`message.content` (str, or blocks with `type`/`text`), `customTitle` / `aiTitle` (title records).
Qwen record keys relied on (Qwen Code 0.21): `type` (user|assistant|tool_result|system), `provenance`
(`real_user` is a human prompt; user records with `provenance == "system"` are injected), `cwd`,
`message.parts[]` with `text`; assistant parts carry `thought: true` for reasoning (not part of the reply)
and `functionCall` for tool calls (no text). The sidecar `<sid>.runtime.json` holds `work_dir`.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_HEAD_START = 65_536
TAIL_MAX_BYTES = 16 * 1024 * 1024  # the backwards read for the last ask/reply never grows past this
_TAGS = ("local-command-caveat", "local-command-stdout", "system-reminder",
         "command-name", "command-message", "command-args", "command-contents")
_TYPE_RES = {  # byte prefilter per store: only lines naming one of its relevant record types are parsed
    "claude": re.compile(rb'"type"\s*:\s*"(user|assistant|ai-title|custom-title)"'),
    "qwen": re.compile(rb'"type"\s*:\s*"(user|assistant|ai-title|custom-title)"'),
    "codex": re.compile(rb'"type"\s*:\s*"(user_message|agent_message)"'),
}


@dataclass
class SessionFacts:
    store: str
    sid: str
    path: Path
    cwd: str | None
    name: str | None
    first_ask: str
    last_ask: str
    last_reply: str
    user_msgs: int
    assistant_msgs: int
    size: int
    mtime: float
    last_ts: float | None = None  # epoch seconds of the last record with a `timestamp` in the tail read, else None
    last_model: str | None = None  # the model of the last assistant record in the tail read (see `_record_model`)


def clean_user_text(s: str) -> str:
    """Strip system/command wrappers so the real ask is left; collapse whitespace (port of claude_sessions)."""
    if not s:
        return ""
    for tag in _TAGS:
        s = re.sub(r"<%s>.*?</%s>" % (tag, tag), " ", s, flags=re.S | re.I)
        s = re.sub(r"</?%s>" % tag, " ", s, flags=re.I)
    s = re.sub(r"Caveat:.*?unless the user explicitly asks.*?\.", " ", s, flags=re.S | re.I)
    s = re.sub(r"</?[a-zA-Z][\w-]*>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def is_top_level_claude(path: Path) -> bool:
    path = Path(path)
    return path.suffix == ".jsonl" and not path.name.startswith("agent-") and "subagents" not in path.parts


# Record parsers: record -> (kind, raw text) with kind in user|assistant|custom-title|ai-title, or None.

def _claude_record(rec: dict) -> tuple[str, str] | None:
    t = rec.get("type")
    if t in ("user", "assistant"):
        msg = rec.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(b.get("text") or "" for b in content if isinstance(b, dict) and b.get("type") == "text")
        else:
            text = ""
        return (t, text) if text.strip() else None
    for kind, key in (("custom-title", "customTitle"), ("ai-title", "aiTitle")):
        if t == kind:
            title = rec.get(key)
            return (kind, title) if isinstance(title, str) and title else None
    return None


def _qwen_record(rec: dict) -> tuple[str, str] | None:
    t = rec.get("type")
    if t not in ("user", "assistant") or (t == "user" and rec.get("provenance") == "system"):
        return None
    msg = rec.get("message")
    parts = msg.get("parts") if isinstance(msg, dict) else None
    if not isinstance(parts, list):
        return None
    text = "\n".join(p["text"] for p in parts
                     if isinstance(p, dict) and not p.get("thought") and isinstance(p.get("text"), str))
    return (t, text) if text.strip() else None


def _codex_record(rec: dict) -> tuple[str, str] | None:
    """Codex rollout line `{"timestamp", "type", "payload"}`: a human prompt is `event_msg` / `user_message`, a reply
    `event_msg` / `agent_message` (both with `payload.message`). `response_item` user records carry injected context
    (environment, AGENTS.md) and never count."""
    payload = rec.get("payload")
    if rec.get("type") != "event_msg" or not isinstance(payload, dict):
        return None
    kind = {"user_message": "user", "agent_message": "assistant"}.get(payload.get("type"))
    text = payload.get("message")
    return (kind, text) if kind and isinstance(text, str) and text.strip() else None


_PARSERS: dict[str, Callable[[dict], tuple[str, str] | None]] = {"claude": _claude_record, "qwen": _qwen_record,
                                                                           "codex": _codex_record}


def _parse(line: bytes, parser) -> tuple[dict, tuple[str, str] | None] | None:
    line = line.strip()
    if not line:
        return None
    try:
        rec = json.loads(line)
    except ValueError:
        return None
    return (rec, parser(rec)) if isinstance(rec, dict) else None


def _head_lines(path: Path, size: int, n: int) -> list[bytes]:
    with open(path, "rb") as fh:
        data = fh.read(n)
    if n < size:  # drop the trailing partial line
        cut = data.rfind(b"\n")
        data = data[:cut] if cut >= 0 else b""
    return data.split(b"\n")


def _tail_lines(path: Path, size: int, n: int) -> list[bytes]:
    start = max(0, size - n)
    with open(path, "rb") as fh:
        fh.seek(start)
        lines = fh.read().split(b"\n")
    return lines[1:] if start > 0 else lines  # the first line of a mid-file read is partial


def _head(path: Path, size: int, parser) -> tuple[str | None, str]:
    """(first cwd seen, cleaned first ask), growing the read by doubling until an ask is found or EOF."""
    n = _HEAD_START
    while True:
        cwd, ask = None, ""
        for line in _head_lines(path, size, n):
            got = _parse(line, parser)
            if got is None:
                continue
            rec, parsed = got
            if cwd is None and isinstance(rec.get("cwd"), str) and rec["cwd"]:
                cwd = rec["cwd"]
            if parsed and parsed[0] == "user":
                ask = clean_user_text(parsed[1])
                if ask:
                    break
        if ask or n >= size:
            return cwd, ask
        n *= 2


def _record_model(rec: dict) -> str | None:
    """The model an assistant record was answered by: `message.model` (Claude Code), else a top-level `model`;
    None for other records and for Claude Code's `<synthetic>` placeholder (local error and notice records)."""
    if rec.get("type") != "assistant":
        return None
    msg = rec.get("message")
    model = msg.get("model") if isinstance(msg, dict) else None
    if not isinstance(model, str) or not model.strip():
        model = rec.get("model")
    if not isinstance(model, str) or not model.strip() or model.strip() == "<synthetic>":
        return None
    return model.strip()


def _tail(path: Path, size: int, parser, tail_bytes: int, want_ask: bool = True) -> tuple[str, str, str | None]:
    """(last cleaned ask, last whole reply, model of the last assistant record), growing backwards by doubling until
    the ask and reply are found, BOF, or the last `TAIL_MAX_BYTES` have been read: then whatever was found is
    returned ("" for the other), so a transcript whose last typed ask lies far back (long tool output since) is never
    read whole. The model comes from the same lines (an assistant record holding only tool calls counts too), so it
    is found whenever the reply is."""
    cap = max(TAIL_MAX_BYTES, 1)
    n = min(max(tail_bytes, 1), cap)
    while True:
        ask = reply = ""
        model = None
        for line in reversed(_tail_lines(path, size, n)):
            got = _parse(line, parser)
            if got is None:
                continue
            if model is None:
                model = _record_model(got[0])
            if got[1] is None:
                continue
            kind, text = got[1]
            if kind == "assistant" and not reply:
                reply = text.strip()
            elif kind == "user" and want_ask and not ask:
                ask = clean_user_text(text)
            if reply and (ask or not want_ask):
                break
        if (reply and (ask or not want_ask)) or n >= size or n >= cap:
            return ask, reply, model
        n = min(n * 2, cap)


def _iso_to_epoch(value) -> float | None:
    """Epoch seconds of an ISO 8601 `timestamp` (`2026-11-01T10:00:00.000Z`; no zone means UTC), else None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00").replace("z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _last_ts(path: Path, size: int, tail_bytes: int) -> float | None:
    """The `timestamp` (Claude and Qwen both write one on each record) of the last record that has one, read
    backwards with the same doubling and cap as `_tail`; None when the tail holds none (the caller uses the file's
    mtime then)."""
    cap = max(TAIL_MAX_BYTES, 1)
    n = min(max(tail_bytes, 1), cap)
    while True:
        for line in reversed(_tail_lines(path, size, n)):
            got = _parse(line, lambda rec: None)
            ts = _iso_to_epoch(got[0].get("timestamp")) if got is not None else None
            if ts is not None:
                return ts
        if n >= size or n >= cap:
            return None
        n = min(n * 2, cap)


def _scan(path: Path, parser, start: int, whole_lines_only: bool, type_re=_TYPE_RES["claude"]) -> tuple[int, int, int, str | None, str | None]:
    """(user asks, assistant replies, end offset, last custom-title, last ai-title) from byte `start` on.
    Only lines naming a relevant type are parsed. With `whole_lines_only`, a last line without its newline (still
    being written) is left out and `end` stops before it, so the next incremental scan picks it up whole."""
    users = assistants = 0
    custom = ai = None
    end = start
    with open(path, "rb") as fh:
        fh.seek(start)
        for line in fh:
            if whole_lines_only and not line.endswith(b"\n"):
                break
            end += len(line)
            if not type_re.search(line):
                continue
            got = _parse(line, parser)
            if got is None or got[1] is None:
                continue
            kind, text = got[1]
            if kind == "user":
                users += 1
            elif kind == "assistant":
                assistants += 1
            elif kind == "custom-title":
                custom = text
            elif kind == "ai-title":
                ai = text
    return users, assistants, end, custom, ai


def _count(path: Path, parser, type_re) -> tuple[int, int, str | None]:
    """(user asks, assistant replies, name) over the whole file."""
    users, assistants, _, custom, ai = _scan(path, parser, 0, whole_lines_only=False, type_re=type_re)
    return users, assistants, custom or ai


def count_messages(path: Path, store: str, start: int = 0) -> tuple[int, int, int, str | None, str | None]:
    """(user asks, assistant replies, end offset, last custom-title, last ai-title) in the complete lines from byte
    `start` on. Pass the previous `end` back as `start` to count only what was appended since."""
    return _scan(Path(path), _PARSERS[store], start, whole_lines_only=True, type_re=_TYPE_RES[store])


def _facts(store: str, path: Path, tail_bytes: int, count: bool = True) -> SessionFacts:
    """With `count=False` only the bounded head/tail reads run: counts are 0 and `name` is None."""
    path = Path(path)
    st = path.stat()
    parser = _PARSERS[store]
    cwd, first = _head(path, st.st_size, parser)
    last_ask, last_reply, last_model = _tail(path, st.st_size, parser, tail_bytes)
    last_ts = _last_ts(path, st.st_size, tail_bytes)
    users, assistants, name = _count(path, parser, _TYPE_RES[store]) if count else (0, 0, None)
    return SessionFacts(store, path.stem, path, cwd, name, first, last_ask, last_reply,
                        users, assistants, st.st_size, st.st_mtime, last_ts, last_model)


def read_claude(path: Path, tail_bytes: int = 262_144, count: bool = True) -> SessionFacts:
    return _facts("claude", path, tail_bytes, count)


def read_qwen(path: Path, tail_bytes: int = 262_144, count: bool = True) -> SessionFacts:
    facts = _facts("qwen", path, tail_bytes, count)
    try:
        work_dir = json.loads(Path(path).with_suffix(".runtime.json").read_text(encoding="utf-8")).get("work_dir")
    except (OSError, ValueError, AttributeError):
        work_dir = None
    if isinstance(work_dir, str) and work_dir:
        facts.cwd = work_dir
    return facts


def read_codex(path: Path, tail_bytes: int = 262_144, count: bool = True) -> SessionFacts:
    """A Codex rollout. It has no top-level `cwd` and its file stem is `rollout-<iso>-<id>`, so `sid` and `cwd`
    come from the first line's `session_meta` (a rollout without one keeps the stem and no cwd)."""
    from codex_store import read_meta  # not at the top: codex_store imports this module
    facts = _facts("codex", path, tail_bytes, count)
    meta = read_meta(Path(path)) or {}
    if isinstance(meta.get("id"), str) and meta["id"]:
        facts.sid = meta["id"]
    cwd = meta.get("cwd")
    facts.cwd = cwd if isinstance(cwd, str) and cwd else None
    return facts


def full_last_reply(path: Path, store: str) -> str:
    """The whole last assistant message (for the preview), read from the end on demand."""
    path = Path(path)
    return _tail(path, path.stat().st_size, _PARSERS[store], 65_536, want_ask=False)[1]
