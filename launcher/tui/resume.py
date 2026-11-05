"""The resume list: a pure renderer (`render_rows`) and the list state the app's keys drive (`ResumeState`).

Nothing here touches the terminal, so both are tested directly. Columns follow the round-3 mockup:
`age(4) name(28) location(fill) tool(7) live(5)` with one blank cell between columns.
"""
from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from prompt_toolkit.utils import get_cwidth

from .. import stores
from ..display import age, middle_ellipsis, rel_or_abs
from ..index import Index, Row
from ..launch import WRAPPERS, Launch, focus_live, resume_command
from ..paths import Env
from .preview import render_preview

AGE_W, NAME_W, TOOL_W, LIVE_W = 4, 28, 7, 5
_FIXED = AGE_W + NAME_W + TOOL_W + LIVE_W + 4  # four 1-cell gaps
_MIN_LOC_W = 8
_MARK = "▶ "
_ELLIPSIS = "…"
_RULE = "─"
COMMANDS = {"n": "new", "o": "open", "w": "wrapper", "q": "quit"}
KEYS = "↑↓ move · type to filter · space preview · enter resume · o open folder · w wrapper · n new · q quit"


@dataclass
class Resume:
    """The user chose to resume `row` with `wrapper`; `launch` is what the shell function runs."""
    row: Row
    wrapper: str
    launch: Launch


class _Signal:
    def __init__(self, name: str):
        self.name = name

    def __repr__(self) -> str:
        return self.name


NEW = _Signal("NEW")  # `n`: start a new session (the cli hands over to the new-session flow)
FOCUSED = _Signal("FOCUSED")  # `enter` on a live row brought its window forward: exit quietly


# ---- rendering -----------------------------------------------------------------------------------------------

def _clean(text: str | None) -> str:
    return "".join(ch if ch.isprintable() else " " for ch in (text or ""))


def fit(text: str | None, width: int) -> str:
    """`text` on one line, cut to `width` terminal cells (ending in `…` when cut) and padded to exactly `width`."""
    text = _clean(text)
    if get_cwidth(text) > width:
        out, used = [], 0
        for ch in text:
            w = get_cwidth(ch)
            if used + w > width - 1:
                break
            out.append(ch)
            used += w
        text = "".join(out) + _ELLIPSIS if width >= 1 else ""
    return text + " " * max(0, width - get_cwidth(text))


def _loc_width(width: int) -> int:
    return max(_MIN_LOC_W, width - _FIXED)


def location(row: Row, root: Path | None, width: int) -> str:
    if not row.cwd:
        return "—"
    shown = rel_or_abs(root, Path(row.cwd)) if root else row.cwd
    return middle_ellipsis(shown, width)


def scroll_top(top: int, selected: int, visible: int) -> int:
    """The first visible row index after the smallest scroll that keeps `selected` on screen."""
    visible = max(1, visible)
    if selected < top:
        return max(0, selected)
    if selected >= top + visible:
        return selected - visible + 1
    return max(0, top)


def render_rows(rows: list[Row], width: int, now: float, selected: int, *, root: Path | None = None,
                top: int = 0, height: int | None = None, status: str = "") -> list[tuple[str, str]]:
    """The whole list screen as prompt_toolkit (style, text) fragments: header, rule, rows, rule, key help and,
    when `status` is set, one status line. With `height`, only the rows that fit are drawn, scrolled from `top`
    just enough to keep `selected` visible (see `scroll_top`)."""
    loc_w = _loc_width(width)
    frags: list[tuple[str, str]] = [
        ("class:header", f"{fit('when', AGE_W)} {fit('session', NAME_W)} {fit('location', loc_w)} "
                         f"{fit('tool', TOOL_W)} {fit('', LIVE_W)}"), ("", "\n"),
        ("class:rule", _RULE * width), ("", "\n")]
    chrome = 5 if status else 4
    visible = len(rows) if height is None else max(1, height - chrome)
    first = scroll_top(top, selected, visible) if height is not None else 0
    shown = rows[first:first + visible]
    if not rows:
        frags += [("class:dim", fit("no sessions match", width)), ("", "\n")]
    for i, row in enumerate(shown, first):
        sel = i == selected
        base = "class:sel " if sel else ""
        name = _MARK + row.display_name if sel else row.display_name
        frags += [(base + "class:dim", fit(age(row.last_activity, now), AGE_W)), (base, " "),
                  (base + ("class:cur" if sel else "class:name"), fit(name, NAME_W)), (base, " "),
                  (base + "class:dim", fit(location(row, root, loc_w), loc_w)), (base, " "),
                  (base + f"class:{row.store}", fit(row.store, TOOL_W)), (base, " "),
                  (base + "class:live", fit("live" if row.live_pid else "", LIVE_W)), ("", "\n")]
    if height is not None:  # keep the footer at the bottom of the screen
        frags += [("", "\n")] * (visible - max(1, len(shown)))
    frags += [("class:rule", _RULE * width), ("", "\n"), ("class:footer", KEYS)]
    if status:
        frags += [("", "\n"), ("class:status", status)]
    return frags


# ---- state ---------------------------------------------------------------------------------------------------

class ResumeState:
    """What the list shows and what each key does, without a terminal. All Index use stays on this (main) thread."""

    def __init__(self, env: Env, index: Index, *, focus: Callable[[Row], dict] = focus_live,
                 popen: Callable[[list[str]], object] = subprocess.Popen):
        self.env = env
        self.index = index
        self._focus = focus
        self._popen = popen
        self.filter = ""
        self.typing = False  # a filter is being typed: letters go to it, not to commands
        self.rows: list[Row] = index.rows()
        self.selected = 0
        self.top = 0
        self.status = ""
        self._wrappers: dict[tuple[str, str], str] = {}  # per-row `w` overrides

    def current(self) -> Row | None:
        return self.rows[self.selected] if self.rows else None

    def move(self, delta: int) -> None:
        if self.rows:
            self.selected = max(0, min(len(self.rows) - 1, self.selected + delta))
        self.status = ""

    def set_filter(self, text: str) -> None:
        self.filter = text
        self.rows = self.index.rows(text)
        self.selected = self.top = 0
        self.status = ""

    # ---- typing ------------------------------------------------------------------------------------------------

    def type_char(self, ch: str) -> str | None:
        """A printable key. While no filter is being typed, `n` `o` `w` `q` are commands (returned as 'new',
        'open', 'wrapper', 'quit') and `/` starts an empty filter (so a filter can begin with one of those
        letters); every other character, and every character once a filter is under way, goes to the filter."""
        if not self.typing:
            if ch in COMMANDS:
                return COMMANDS[ch]
            if ch == "/":
                self.typing = True
                self.status = ""
                return None
        self.typing = True
        self.set_filter(self.filter + ch)
        return None

    def backspace(self) -> None:
        if self.filter:
            self.set_filter(self.filter[:-1])
        if not self.filter:
            self.typing = False

    def escape(self) -> bool:
        """Clear the filter; False when there was none (the app then quits)."""
        if not (self.typing or self.filter):
            return False
        self.typing = False
        if self.filter:
            self.set_filter("")
        return True

    def footer_status(self) -> str:
        parts = [f"filter: {self.filter}▏"] if self.typing else []
        if self.status:
            parts.append(self.status)
        return " · ".join(parts)

    # ---- wrapper and launch ------------------------------------------------------------------------------------

    def launch_for(self, row: Row) -> Launch:
        return resume_command(row, self._wrappers.get((row.store, row.sid)), root=self.env.root)

    def wrapper_for(self, row: Row) -> str:
        return self.launch_for(row).argv[0]

    def cycle_wrapper(self) -> None:
        row = self.current()
        if row is None:
            return
        if row.store == "qwen":
            self.status = "Qwen sessions always resume with qwen"
            return
        choices = [w for w in WRAPPERS if w != "qwen"]
        now = self.wrapper_for(row)
        nxt = choices[(choices.index(now) + 1) % len(choices)] if now in choices else choices[0]
        self._wrappers[(row.store, row.sid)] = nxt
        self.status = f"resume with {nxt}"

    def enter(self) -> Resume | _Signal | None:
        """A `Resume` to exit with, `FOCUSED` when a live session's window was brought forward, or None to stay
        (nothing selected, or focusing failed: the error is in `status`)."""
        row = self.current()
        if row is None:
            return None
        if row.live_pid is not None:
            res = self._focus(row)
            if res.get("ok"):
                return FOCUSED
            self.status = f"could not focus pid {row.live_pid}: {res.get('error') or 'unknown error'}"
            return None
        launch = self.launch_for(row)
        return Resume(row, launch.argv[0], launch)

    def open_folder(self) -> None:
        row = self.current()
        if row is None:
            return
        if not row.cwd or not row.cwd_exists:
            self.status = f"folder no longer exists: {row.cwd}" if row.cwd else "session has no recorded folder"
            return
        try:
            self._popen(["explorer.exe", row.cwd])
            self.status = f"opened {row.cwd}"
        except OSError as exc:
            self.status = f"could not open folder: {exc}"

    # ---- preview -----------------------------------------------------------------------------------------------

    def preview_text(self, width: int) -> str:
        row = self.current()
        if row is None:
            return ""
        reply = row.last_reply
        if row.path:
            try:
                reply = stores.full_last_reply(Path(row.path), row.store) or reply
            except (OSError, ValueError):
                pass  # moved or locked since the last refresh: the indexed first 400 chars will do
        return render_preview(row, reply, width, launch=None if row.live_pid else self.launch_for(row))
