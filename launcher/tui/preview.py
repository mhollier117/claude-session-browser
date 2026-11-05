"""The full-screen preview: a pure renderer from an index row (plus its whole last reply) to wrapped text."""
from __future__ import annotations

import textwrap
from datetime import datetime

from ..index import Row
from ..launch import Launch

FOOTER = "space/esc back · ↑↓ scroll · enter resume · o open folder"
_INDENT = "  "
_NONE = "(none)"


def human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 ** 2:
        return f"{size // 1024} KB"
    if size < 1024 ** 3:
        return f"{size // 1024 ** 2} MB"
    return f"{size / 1024 ** 3:.1f} GB"


def _wrap(text: str, width: int, indent: str = _INDENT) -> list[str]:
    """`text` wrapped to `width` columns (indent included), keeping its own line breaks and blank lines."""
    out: list[str] = []
    for para in (text or "").replace("\r\n", "\n").replace("\t", "    ").split("\n"):
        if not para.strip():
            out.append("")
            continue
        out += textwrap.wrap(para, max(10, width), initial_indent=indent, subsequent_indent=indent,
                             break_long_words=True, break_on_hyphens=False, drop_whitespace=True) or [""]
    while out and not out[-1]:
        out.pop()
    while out and not out[0]:
        out.pop(0)
    return out or [indent + _NONE]


def _arg(a: str) -> str:
    """One argv item as PowerShell would need it typed."""
    return "'" + a.replace("'", "''") + "'" if not a or any(c in a for c in " \t'\"`$;&|(){}@,") else a


def command_lines(row: Row, launch: Launch | None) -> list[str]:
    """What `enter` will do, as the lines shown under the `enter runs` heading."""
    if row.live_pid is not None:
        return [f"focus the live session's window (pid {row.live_pid})"]
    if launch is None:
        return []
    return [f"cd {_arg(str(launch.cwd))}", " ".join(_arg(a) for a in launch.argv)] + \
        [f"warning: {w}" for w in launch.warnings]


def render_preview(row: Row, full_reply: str, width: int, *, launch: Launch | None = None) -> str:
    """Name and meta, full path, first ask, last ask, last reply (whole), the command `enter` runs, footer.
    Every line fits in `width - 2` columns."""
    w = max(20, width - 2)
    stamp = datetime.fromtimestamp(row.last_activity).strftime("%Y-%m-%d %H:%M") if row.last_activity else "—"
    meta = f"{row.store} · {stamp} · {row.user_msgs + row.assistant_msgs} messages · {human_size(row.size)}"
    name = " ".join((row.display_name or "").split())
    if len(name) + 3 + len(meta) <= w:
        lines = [f"{name}   {meta}"]
    else:
        lines = _wrap(name, w, "") + _wrap(meta, w, "")
    path = row.cwd or "(no recorded folder)"
    if row.cwd and not row.cwd_exists:
        path += "  (folder no longer exists)"
    lines += _wrap(path, w, "")
    for title, text in (("first ask", row.first_ask), ("last ask", row.last_ask), ("last reply", full_reply)):
        lines += ["", title] + _wrap(text, w)
    cmd = command_lines(row, launch)
    if cmd:
        lines += ["", "enter runs"] + [ln for c in cmd for ln in _wrap(c, w)]
    lines += ["", FOOTER]
    return "\n".join(lines)
