"""Small formatting helpers for the launcher screens."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

_SEP = "\\"
_ELLIPSIS = "…"
_NO_DATE = "—"


def rel_or_abs(root: Path, path: Path) -> str:
    """`path` relative to `root` when it sits under it ('.' for the root itself), else the absolute path."""
    try:
        return str(Path(path).relative_to(root))
    except ValueError:
        return str(path)


def _truncate_head(head: str, room: int) -> str:
    return head[:room - 1] + _ELLIPSIS if room >= 2 else ""


def middle_ellipsis(path_str: str, width: int) -> str:
    """Fit `path_str` into `width` characters: keep the first segment and the last two, replace the middle
    with `…`. If that is still too wide, shorten the first segment (dropping it if need be); the last two
    segments are never cut."""
    if len(path_str) <= width:
        return path_str
    segs = path_str.split(_SEP)
    if len(segs) == 1:
        return path_str
    first = segs[0]
    if len(segs) <= 3:  # first plus the last two already is the whole path: nothing to drop in the middle
        middle, tail = _SEP, _SEP.join(segs[1:])
    else:
        middle, tail = _SEP + _ELLIPSIS + _SEP, _SEP.join(segs[-2:])
    if len(first) + len(middle) + len(tail) <= width:
        return first + middle + tail
    head = _truncate_head(first, width - len(middle) - len(tail))
    return head + middle + tail if head else _ELLIPSIS + _SEP + tail


def age(ts: float | None, now: float) -> str:
    """'now', '5m', '2h', '3d'; a month abbreviation beyond 30 days; an em dash when there is no timestamp."""
    if ts is None:
        return _NO_DATE
    delta = max(0.0, now - ts)
    if delta < 60:
        return "now"
    if delta < 3600:
        return f"{int(delta // 60)}m"
    if delta < 86400:
        return f"{int(delta // 3600)}h"
    if delta <= 30 * 86400:
        return f"{int(delta // 86400)}d"
    return datetime.fromtimestamp(ts).strftime("%b")
