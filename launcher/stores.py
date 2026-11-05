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
from pathlib import Path

_HEAD_START = 65_536
_TAGS = ("local-command-caveat", "local-command-stdout", "system-reminder",
         "command-name", "command-message", "command-args", "command-contents")
_TYPE_RE = re.compile(rb'"type"\s*:\s*"(user|assistant|ai-title|custom-title)"')


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


_PARSERS: dict[str, Callable[[dict], tuple[str, str] | None]] = {"claude": _claude_record, "qwen": _qwen_record}


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


def _tail(path: Path, size: int, parser, tail_bytes: int, want_ask: bool = True) -> tuple[str, str]:
    """(last cleaned ask, last whole reply), growing backwards by doubling until both are found or BOF."""
    n = max(tail_bytes, 1)
    while True:
        ask = reply = ""
        for line in reversed(_tail_lines(path, size, n)):
            got = _parse(line, parser)
            if got is None or got[1] is None:
                continue
            kind, text = got[1]
            if kind == "assistant" and not reply:
                reply = text.strip()
            elif kind == "user" and want_ask and not ask:
                ask = clean_user_text(text)
            if reply and (ask or not want_ask):
                break
        if (reply and (ask or not want_ask)) or n >= size:
            return ask, reply
        n *= 2


def _count(path: Path, parser) -> tuple[int, int, str | None]:
    """(user asks, assistant replies, name) over the whole file; only lines naming a relevant type are parsed."""
    users = assistants = 0
    custom = ai = None
    with open(path, "rb") as fh:
        for line in fh:
            if not _TYPE_RE.search(line):
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
    return users, assistants, custom or ai


def _facts(store: str, path: Path, tail_bytes: int) -> SessionFacts:
    path = Path(path)
    st = path.stat()
    parser = _PARSERS[store]
    cwd, first = _head(path, st.st_size, parser)
    last_ask, last_reply = _tail(path, st.st_size, parser, tail_bytes)
    users, assistants, name = _count(path, parser)
    return SessionFacts(store, path.stem, path, cwd, name, first, last_ask, last_reply,
                        users, assistants, st.st_size, st.st_mtime)


def read_claude(path: Path, tail_bytes: int = 262_144) -> SessionFacts:
    return _facts("claude", path, tail_bytes)


def read_qwen(path: Path, tail_bytes: int = 262_144) -> SessionFacts:
    facts = _facts("qwen", path, tail_bytes)
    try:
        work_dir = json.loads(Path(path).with_suffix(".runtime.json").read_text(encoding="utf-8")).get("work_dir")
    except (OSError, ValueError, AttributeError):
        work_dir = None
    if isinstance(work_dir, str) and work_dir:
        facts.cwd = work_dir
    return facts


def full_last_reply(path: Path, store: str) -> str:
    """The whole last assistant message (for the preview), read from the end on demand."""
    path = Path(path)
    return _tail(path, path.stat().st_size, _PARSERS[store], 65_536, want_ask=False)[1]
