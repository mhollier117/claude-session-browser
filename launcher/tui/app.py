"""The prompt_toolkit application for `cc`: it only maps keys onto `ResumeState` and draws the pure renderers.

The PowerShell `cc` function captures stdout to read the launch line, so the UI cannot draw on stdout. When
stdout is not a terminal the app opens the console itself (`CONOUT$`) and, for the duration of the run, makes it
the process's standard output handle (prompt_toolkit's Windows output queries that handle directly). Python's
`sys.stdout` keeps pointing at the captured pipe, so the JSON line still lands there afterwards.
"""
from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

from prompt_toolkit.application import Application
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.output import Output, create_output
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import TextArea

from ..index import Index
from ..paths import Env
from .preview import FOOTER as PREVIEW_FOOTER
from .resume import FOCUSED, NEW, Resume, ResumeState, render_rows, scroll_top

if TYPE_CHECKING:  # imported where used, so the resume list does not pay for the new-session modules
    from ..launch import Launch
    from .new import NewFlow

STYLE = Style.from_dict({
    "header": "#a3abb8", "rule": "#343a46", "dim": "#a3abb8", "name": "#e3e7ec",
    "sel": "bg:#2b3240", "cur": "bg:#e3e7ec #1b1e24",
    "claude": "#8ab4f8", "qwen": "#ffd27a", "live": "#8fd19e",
    "footer": "#a3abb8", "status": "#ffd27a",
    "title": "#ffd27a", "section": "#8ab4f8",
})
_SECTIONS = {"first ask", "last ask", "last reply", "enter runs"}


class NoTerminal(RuntimeError):
    """`cc` was started without a console to draw on."""


class _PreviewLexer(Lexer):
    def lex_document(self, document: Document):
        lines = document.lines

        def get_line(i: int):
            line = lines[i]
            if i == 0:
                return [("class:title", line)]
            if line in _SECTIONS:
                return [("class:section", line)]
            if line == PREVIEW_FOOTER or i == 1:
                return [("class:dim", line)]
            return [("", line)]
        return get_line


@contextmanager
def console_output() -> Iterator[Output]:
    """A prompt_toolkit Output on the terminal even when stdout is redirected (see the module docstring)."""
    if not (sys.stdin and sys.stdin.isatty()):
        raise NoTerminal("needs an interactive terminal (stdin is not a console)")
    if sys.stdout is not None and sys.stdout.isatty():
        yield create_output()
        return
    if sys.platform != "win32":
        yield create_output(always_prefer_tty=True)
        return
    import ctypes
    import msvcrt
    import os
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.GetStdHandle.restype = wintypes.HANDLE
    k32.GetStdHandle.argtypes = [wintypes.DWORD]
    k32.SetStdHandle.argtypes = [wintypes.DWORD, wintypes.HANDLE]
    std_output = wintypes.DWORD(-11 & 0xFFFFFFFF)
    # read access too: prompt_toolkit asks the handle for the screen buffer size
    handle = k32.CreateFileW("CONOUT$", 0x80000000 | 0x40000000, 0x1 | 0x2, None, 3, 0, None)
    if not handle or handle == wintypes.HANDLE(-1).value:
        raise NoTerminal("needs an interactive terminal (no console to draw on)")
    stream = open(msvcrt.open_osfhandle(handle, os.O_WRONLY), "w", encoding="utf-8")
    previous = k32.GetStdHandle(std_output)
    k32.SetStdHandle(std_output, handle)
    try:
        yield create_output(stdout=stream)
    finally:
        k32.SetStdHandle(std_output, previous)
        stream.close()


def build_app(env: Env, index: Index, output: Output | None = None, input=None) -> Application:
    state = ResumeState(env, index)
    mode = {"preview": False}
    in_list = Condition(lambda: not mode["preview"])
    in_preview = Condition(lambda: mode["preview"])

    def size():
        return app.output.get_size()

    def chrome() -> int:
        return 5 if state.footer_status() else 4

    def list_fragments():
        s = size()
        state.top = scroll_top(state.top, state.selected, s.rows - chrome())
        return render_rows(state.rows, s.columns, env.now(), state.selected, root=env.root, top=state.top,
                           height=s.rows, status=state.footer_status())

    list_window = Window(FormattedTextControl(list_fragments, focusable=True, show_cursor=False))
    preview = TextArea(read_only=True, scrollbar=True, wrap_lines=False,  # render_preview wraps by cells
                        lexer=_PreviewLexer(), focusable=True)
    preview.window.always_hide_cursor = Condition(lambda: True)

    kb = KeyBindings()

    def show_list(event) -> None:
        mode["preview"] = False
        event.app.layout.focus(list_window)

    def scroll(delta: int) -> None:
        win = preview.window
        info = win.render_info
        height = info.window_height if info else size().rows
        top = max(0, min(max(0, preview.document.line_count - height), win.vertical_scroll + delta))
        win.vertical_scroll = top
        # the cursor sits on the top line, so the window keeps exactly this scroll position
        preview.buffer.cursor_position = preview.document.translate_row_col_to_index(top, 0)

    def page() -> int:
        return max(1, size().rows - chrome())

    @kb.add("c-c")
    def _quit(event) -> None:
        event.app.exit(result=None)

    @kb.add("enter")
    def _enter(event) -> None:
        result = state.enter()
        if isinstance(result, Resume) or result is FOCUSED:
            event.app.exit(result=result)
        elif mode["preview"] and state.status:
            show_list(event)  # the focus error is shown in the list footer

    @kb.add("up", filter=in_list)
    def _up(event) -> None:
        state.move(-1)

    @kb.add("down", filter=in_list)
    def _down(event) -> None:
        state.move(1)

    @kb.add("pageup", filter=in_list)
    def _pgup(event) -> None:
        state.move(-page())

    @kb.add("pagedown", filter=in_list)
    def _pgdn(event) -> None:
        state.move(page())

    @kb.add("space", filter=in_list)
    def _preview(event) -> None:
        if state.current() is None:
            return
        preview.document = Document(state.preview_text(size().columns), 0)
        preview.window.vertical_scroll = 0
        mode["preview"] = True
        event.app.layout.focus(preview)

    @kb.add("backspace", filter=in_list)
    def _backspace(event) -> None:
        state.backspace()

    @kb.add("escape", filter=in_list, eager=True)  # eager: arrows are decoded before this, so no wait
    def _escape(event) -> None:
        state.escape()  # clears an active filter; only q and ctrl-c quit

    @kb.add("<any>", filter=in_list)
    def _typed(event) -> None:
        ch = event.data
        if len(ch) != 1 or not ch.isprintable():
            return
        command = state.type_char(ch)
        if command == "quit":
            event.app.exit(result=None)
        elif command == "new":
            event.app.exit(result=NEW)
        elif command == "open":
            state.open_folder()
        elif command == "wrapper":
            state.cycle_wrapper()

    @kb.add("space", filter=in_preview)
    @kb.add("escape", filter=in_preview, eager=True)
    def _back(event) -> None:
        show_list(event)

    @kb.add("up", filter=in_preview)
    def _sup(event) -> None:
        scroll(-1)

    @kb.add("down", filter=in_preview)
    def _sdown(event) -> None:
        scroll(1)

    @kb.add("pageup", filter=in_preview)
    def _spgup(event) -> None:
        scroll(-page())

    @kb.add("pagedown", filter=in_preview)
    def _spgdn(event) -> None:
        scroll(page())

    @kb.add("o", filter=in_preview)
    def _open(event) -> None:
        state.open_folder()

    @kb.add("<any>", filter=in_preview)
    def _ignore(event) -> None:
        pass  # the preview is read-only: swallow other keys instead of ringing the bell

    root = HSplit([ConditionalContainer(list_window, filter=in_list),
                   ConditionalContainer(preview, filter=in_preview)])
    app = Application(layout=Layout(root, focused_element=list_window), key_bindings=kb, style=STYLE,
                      full_screen=True, mouse_support=False, output=output, input=input)
    app.ttimeoutlen = 0.05  # a lone esc is not held 0.5 s waiting for an escape sequence (arrows arrive whole)
    return app


def run_resume(env: Env, index: Index):
    """Show the resume list until the user chooses; returns a `Resume`, `NEW`, `FOCUSED` or None (quit)."""
    with console_output() as output:
        return build_app(env, index, output=output).run()


# ---- the new-session walk ----------------------------------------------------------------------------------------

POLL_S = 0.25  # how often the picker looks for the model's suggestion


def build_new_app(flow: NewFlow, output: Output | None = None, input=None) -> Application:
    """Draw `render_new(flow)` and route keys to it; a 250 ms timer applies the model's suggestion via `flow.poll`
    (it never blocks: `Pending.wait(0)`). Exits with the flow's `Launch`, or None on ctrl-c (or q on a choice
    step; in the picker q is a filter character)."""
    from .new import render_new
    kb = KeyBindings()
    texty = Condition(lambda: flow.step in ("name", "folder_name"))

    def fragments():
        s = app.output.get_size()
        return render_new(flow, s.columns, s.rows)

    def after(event) -> None:
        if flow.step == "done":
            event.app.exit(result=flow.result())

    @kb.add("c-c")
    def _quit(event) -> None:
        event.app.exit(result=None)

    busy = {"on": False}

    @kb.add("enter")
    def _enter(event) -> None:
        if busy["on"]:
            return
        target = flow.creating()
        if target is None:
            flow.enter()
            after(event)
            return

        async def create() -> None:  # draw the status first: git init / gh repo create block for seconds
            busy["on"] = True
            flow.status = f"creating {target}…"
            event.app.invalidate()
            await asyncio.sleep(0.05)
            try:
                flow.enter()
            finally:
                flow.status = ""
                busy["on"] = False
            event.app.invalidate()
            after(event)

        event.app.create_background_task(create())

    @kb.add("escape", eager=True)
    def _escape(event) -> None:
        flow.esc()

    @kb.add("up", filter=~texty)
    def _up(event) -> None:
        flow.move(-1)

    @kb.add("down", filter=~texty)
    def _down(event) -> None:
        flow.move(1)

    @kb.add("backspace")
    def _backspace(event) -> None:
        if flow.step in ("name", "folder_name"):
            flow.text = flow.text[:-1]
            flow.error = ""
        elif flow.step == "picker":
            flow.backspace()

    @kb.add("<any>")
    def _typed(event) -> None:
        ch = event.data
        if len(ch) != 1 or not ch.isprintable():
            return
        if flow.step in ("name", "folder_name"):
            flow.text += ch
            flow.error = ""
        elif flow.step == "picker":  # every printable key filters, q included (folder names start with q too)
            flow.type_filter(flow.filter + ch)
        elif ch == "q":  # the kind, wrapper and GitHub choices have no text to type
            event.app.exit(result=None)

    window = Window(FormattedTextControl(fragments, focusable=True, show_cursor=False), wrap_lines=False)
    app = Application(layout=Layout(window), key_bindings=kb, style=STYLE, full_screen=True, mouse_support=False,
                      output=output, input=input)
    app.ttimeoutlen = 0.05
    return app


async def _poll_suggestion(app: Application, flow: NewFlow) -> None:
    """Every `POLL_S`: apply the model's answer if it has come (`flow.poll` never waits) and redraw, which also
    keeps the "asking the model" line current. Cancelled when the app exits."""
    while True:
        await asyncio.sleep(POLL_S)
        flow.poll()
        app.invalidate()


def run_new(env: Env, index: Index, name: str | None) -> Launch | None:
    """The new-session walk on the console; `name` (from `cc new <name>`) skips the name prompt."""
    from ..scaffold import Runner
    from .new import NewFlow
    flow = NewFlow(env, index, Runner())
    if name:
        flow.start(name)
    with console_output() as output:
        app = build_new_app(flow, output=output)
        return app.run(pre_run=lambda: app.create_background_task(_poll_suggestion(app, flow)))
