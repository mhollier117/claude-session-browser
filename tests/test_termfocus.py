"""termfocus.py: bring a session's terminal window to the front and pick its tab.

Win32, psutil and UI Automation are replaced by fakes, so nothing on the real
desktop moves.
"""

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import termfocus as tf  # noqa: E402

WT, PSEUDO, CONHOST = tf.WT_CLASS, tf.PSEUDO_CLASS, tf.CONHOST_CLASS
CLAUDE, SHELL, SHELL2, TERM = 100, 90, 91, 80
HOST, HOST2 = 0xA0, 0xB0


class FakeWin:
    """Desktop model: console windows, owners, classes, console titles and
    which way of getting to the foreground works."""

    def __init__(self):
        self.consoles = []          # (hwnd, class, first console pid)
        self.roots = {}             # hwnd -> root owner
        self.classes = {HOST: WT, HOST2: WT}
        self.visible = set()
        self.tops = {}              # pid -> [(hwnd, pid, title)]
        self.console = {}           # pid -> {"hwnd", "title", "original"}
        self.fg = 0x1
        self.accept = {"plain"}     # steps that bring a window to the front
        self.iconic = set()
        self.calls = []
        self._alt = False

    def console_windows(self):
        return list(self.consoles)

    def top_windows(self, pids):
        return [w for p in pids for w in self.tops.get(p, [])]

    def class_name(self, h):
        return self.classes.get(h, "")

    def title(self, h):
        return ""

    def root_owner(self, h):
        return self.roots.get(h, h)

    def is_visible(self, h):
        return h in self.visible

    def _con(self, pid):
        c = self.console.get(pid)
        if c is None:
            raise OSError("access denied")
        return c

    def console_info(self, pid):
        self.calls.append(("attach", pid))
        return dict(self._con(pid))

    def swap_title(self, pid, marker):
        c = self._con(pid)
        self.calls.append(("swap", marker))
        saved, c["title"] = c["title"], marker
        return saved

    def restore_title(self, pid, marker, saved):
        c = self._con(pid)
        self.calls.append(("restore", saved))
        if c["title"] != marker:
            return False
        c["title"] = saved
        return True

    def is_iconic(self, h):
        return h in self.iconic

    def restore(self, h):
        self.calls.append(("restore_window", h))
        self.iconic.discard(h)

    def foreground(self):
        return self.fg

    def set_foreground(self, h):
        step = "alt" if self._alt else "plain"
        self.calls.append(("setfg", step))
        if step in self.accept:
            self.fg = h
        return step in self.accept

    def alt_tap(self):
        self.calls.append(("alt",))
        self._alt = True

    def set_foreground_attached(self, h):
        self.calls.append(("setfg", "attach"))
        if "attach" in self.accept:
            self.fg = h
        return "attach" in self.accept


class FakeProcs:
    def __init__(self, chain, cwd=""):
        self.chain, self._cwd = chain, cwd

    def ancestors(self, pid):
        return list(self.chain)

    def cwd(self, pid):
        return self._cwd


class FakeTab:
    def __init__(self, uia, name, key):
        self.uia, self.name, self.key = uia, name, key

    def selected(self):
        return self.uia.selected == self.key

    def select(self):
        self.uia.selected = self.key
        return True


class FakeUia:
    """Tabs of one host: a pid shows its console title (a new title only after
    `lag` lookups, like Windows Terminal), a string is a fixed tab name."""

    def __init__(self, win, layout, lag=0, fail=False):
        self.win, self.layout, self.lag, self.fail = win, layout, lag, fail
        self.selected = None
        self.lookups = 0
        self._shown = {}

    def tabs(self, hwnd):
        self.lookups += 1
        if self.fail:
            raise RuntimeError("uia broke")
        out = []
        for i, item in enumerate(self.layout.get(hwnd, [])):
            if isinstance(item, int):
                now = self.win.console[item]["title"]
                shown = self._shown.setdefault(item, [now, 0])
                if shown[0] != now:
                    shown[1] += 1
                    if shown[1] > self.lag:
                        shown[:] = [now, 0]
                out.append(FakeTab(self, shown[0], i))
            else:
                out.append(FakeTab(self, item, i))
        return out


@pytest.fixture
def env(monkeypatch):
    win = FakeWin()
    procs = FakeProcs([(CLAUDE, "claude.exe"), (SHELL, "pwsh.exe"),
                       (TERM, "WindowsTerminal.exe")])
    loads = []
    box = {"uia": None}

    def factory():
        loads.append(1)
        if box["uia"] is None:
            raise ImportError("no pythonnet here")
        return box["uia"]

    monkeypatch.setattr(tf, "_win_obj", win)
    monkeypatch.setattr(tf, "_procs", procs)
    monkeypatch.setattr(tf, "_Uia", factory)
    monkeypatch.setattr(tf, "_uia_obj", None)
    monkeypatch.setattr(tf, "_uia_error", None)
    monkeypatch.setattr(tf, "_last_at", None)
    monkeypatch.setattr(tf, "POLL_S", 0.005)
    return win, procs, box, loads


def wt_window(win, own_tabs=1, other_tabs=0):
    """The session's shell console in HOST (plus more consoles there), and one
    unrelated console in HOST2."""
    win.consoles = [(0x10, PSEUDO, SHELL)]
    win.roots[0x10] = HOST
    for i in range(own_tabs - 1):
        win.consoles.append((0x30 + i, PSEUDO, 500 + i))
        win.roots[0x30 + i] = HOST
    win.consoles.append((0x20, PSEUDO, SHELL2))
    win.roots[0x20] = HOST2
    for i in range(other_tabs):
        win.consoles.append((0x40 + i, PSEUDO, 600 + i))
        win.roots[0x40 + i] = HOST2


def test_strip_removes_the_status_glyph():
    assert tf._strip("✳ Clawdmeter") == "Clawdmeter"
    assert tf._strip("◐ Clawdmeter") == "Clawdmeter"
    assert tf._strip("Claude Code") == "Claude Code"
    assert tf._strip(None) == ""


def test_window_found_through_the_shell_ancestor_single_tab_skips_uia(env):
    win, _, _, loads = env
    wt_window(win, own_tabs=1, other_tabs=2)
    r = tf.focus_session(CLAUDE)
    assert r["ok"] and r["window"] and r["tab"] == "single" and r["via"] == "plain"
    assert win.fg == HOST
    assert loads == []                                    # pythonnet never loaded
    assert not any(c[0] == "attach" for c in win.calls)   # no AttachConsole either


def test_nearest_ancestor_wins(env):
    win, _, _, _ = env
    win.consoles = [(0x20, PSEUDO, TERM), (0x10, PSEUDO, SHELL)]
    win.roots = {0x10: HOST, 0x20: HOST2}
    assert tf._resolve(CLAUDE)["host"] == HOST


def test_attach_fallback_when_no_console_matches(env):
    win, _, _, _ = env
    win.consoles = [(0x20, PSEUDO, 777)]
    win.roots = {0x20: HOST2, 0x10: HOST}
    win.console[CLAUDE] = {"hwnd": 0x10, "title": "x", "original": "y"}
    d = tf._resolve(CLAUDE)
    assert d["host"] == HOST and d["via"] == "attach" and d["kind"] == "wt"


def test_classic_conhost_window(env):
    win, _, _, loads = env
    win.consoles = [(0x50, CONHOST, SHELL)]
    win.classes[0x50] = CONHOST
    r = tf.focus_session(CLAUDE)
    assert r["ok"] and r["tab"] == "single" and win.fg == 0x50 and loads == []


def multi(env, titles, lag=0, fail=False):
    """HOST with three tabs: the session's console + two fixed ones."""
    win, _, box, _ = env
    wt_window(win, own_tabs=3)
    win.console[CLAUDE] = {"hwnd": 0x10, "title": titles[0], "original": titles[1]}
    others = list(titles[2:]) or ["PowerShell 7", "Other"]
    uia = box["uia"] = FakeUia(win, {HOST: [others[0], CLAUDE, others[1]]}, lag=lag, fail=fail)
    return win, uia


def test_tab_exact_title(env):
    win, uia = multi(env, ("Claude Code", "PowerShell 7", "PowerShell 7", "✳ Other"))
    r = tf.focus_session(CLAUDE)
    assert r["ok"] and r["tab"] == "exact" and uia.selected == 1
    assert not any(c[0] == "swap" for c in win.calls)


def test_tab_title_without_glyph(env):
    # Spinner moved between reading the title and reading the tabs.
    win, uia = multi(env, ("◐ Clawdmeter", "PowerShell 7"))
    uia._shown[CLAUDE] = ["◑ Clawdmeter", 0]
    uia.lag = 99
    r = tf.focus_session(CLAUDE)
    assert r["tab"] == "stripped" and uia.selected == 1


def test_tab_marker_when_titles_are_ambiguous_and_title_is_restored(env):
    win, uia = multi(env, ("Claude Code", "PowerShell 7", "Claude Code", "PowerShell 7"), lag=2)
    r = tf.focus_session(CLAUDE)
    assert r["ok"] and r["tab"] == "marker" and uia.selected == 1
    assert win.console[CLAUDE]["title"] == "Claude Code"              # restored
    swaps = [c for c in win.calls if c[0] in ("swap", "restore")]
    assert swaps[0][0] == "swap" and swaps[0][1].startswith("CSB-")
    assert swaps[1] == ("restore", "Claude Code")


def test_marker_not_seen_falls_back_to_original_title_and_restores(env, monkeypatch):
    monkeypatch.setattr(tf, "MARKER_WAIT_S", 0.05)
    # Two tabs say "Claude Code", the marker never shows up and no tab carries
    # the original title: no tab change, title back as it was.
    win, uia = multi(env, ("Claude Code", "my-profile", "Claude Code", "PowerShell 7"),
                     lag=10_000)
    r = tf.focus_session(CLAUDE)
    assert r["ok"] and r["tab"] == "none" and uia.selected is None
    assert win.console[CLAUDE]["title"] == "Claude Code"
    assert "marker not seen" in r["note"]


def test_original_title_when_marker_fails(env, monkeypatch):
    monkeypatch.setattr(tf, "MARKER_WAIT_S", 0.05)
    win, uia = multi(env, ("Claude Code", "tv-3d", "Claude Code", "tv-3d"), lag=10_000)
    # Profile with a fixed tab title: the tab shows the original title, never
    # the marker; two other tabs make "Claude Code" ambiguous.
    uia.layout[HOST] = ["Claude Code", "tv-3d", "Claude Code"]
    r = tf.focus_session(CLAUDE)
    assert r["tab"] == "original" and uia.selected == 1
    assert win.console[CLAUDE]["title"] == "Claude Code"


def test_marker_restored_when_uia_fails_mid_way(env):
    win, uia = multi(env, ("Claude Code", "PowerShell 7", "Claude Code", "PowerShell 7"))
    real = uia.tabs

    def flaky(hwnd):
        if any(c[0] == "swap" for c in win.calls):
            raise RuntimeError("uia broke")
        return real(hwnd)
    uia.tabs = flaky
    r = tf.focus_session(CLAUDE)
    assert r["ok"] and r["tab"] == "none" and "uia broke" in r["note"]
    assert win.console[CLAUDE]["title"] == "Claude Code"


def test_title_changed_by_the_app_is_left_alone(env):
    win, uia = multi(env, ("Claude Code", "x", "Claude Code", "x"), lag=10_000)
    real_swap = win.swap_title

    def swap(pid, marker):
        saved = real_swap(pid, marker)
        win.console[pid]["title"] = "✳ new title"        # Claude Code sets its own
        return saved
    win.swap_title = swap
    tf.focus_session(CLAUDE)
    assert win.console[CLAUDE]["title"] == "✳ new title"


def test_no_title_access_means_window_only(env):
    # e.g. an elevated session: AttachConsole is refused.
    win, _, box, loads = env
    wt_window(win, own_tabs=3)
    box["uia"] = FakeUia(win, {})
    r = tf.focus_session(CLAUDE)
    assert r["ok"] and r["window"] and r["tab"] == "none" and "access denied" in r["note"]
    assert loads == []


def test_uia_unavailable_means_window_only(env):
    win, _, _, loads = env
    wt_window(win, own_tabs=2)
    win.console[CLAUDE] = {"hwnd": 0x10, "title": "a", "original": "b"}
    r = tf.focus_session(CLAUDE)
    assert r["ok"] and r["tab"] == "none" and "ImportError" in r["note"]
    tf._last_at = None
    tf.focus_session(CLAUDE)
    assert loads == [1]                       # a failed load is not retried


def test_uia_is_loaded_once_and_reused(env):
    win, uia = multi(env, ("Claude Code", "x"))
    _, _, _, loads = env
    tf.focus_session(CLAUDE)
    tf._last_at = None
    tf.focus_session(CLAUDE)
    assert loads == [1]


def test_alt_tap_when_plain_set_foreground_is_refused(env):
    win, _, _, _ = env
    wt_window(win)
    win.accept = {"alt"}
    r = tf.focus_session(CLAUDE)
    assert r["ok"] and r["via"] == "alt"
    assert [c for c in win.calls if c[0] in ("setfg", "alt")] == [
        ("setfg", "plain"), ("alt",), ("setfg", "alt")]


def test_attach_thread_input_as_last_resort(env):
    win, _, _, _ = env
    wt_window(win)
    win.accept = {"attach"}
    r = tf.focus_session(CLAUDE)
    assert r["ok"] and r["via"] == "attach"


def test_window_that_never_comes_forward(env):
    win, _, _, _ = env
    wt_window(win)
    win.accept = set()
    r = tf.focus_session(CLAUDE)
    assert not r["ok"] and not r["window"] and r["error"]


def test_minimized_window_is_restored_only_when_minimized(env):
    win, _, _, _ = env
    wt_window(win)
    tf.focus_session(CLAUDE)
    assert not any(c[0] == "restore_window" for c in win.calls)
    win.iconic.add(HOST)
    win.fg = 0x1
    tf._last_at = None
    tf.focus_session(CLAUDE)
    assert ("restore_window", HOST) in win.calls


def test_repeats_within_a_second_are_ignored(env):
    win, _, _, _ = env
    wt_window(win)
    assert tf.focus_session(CLAUDE)["ok"]
    r = tf.focus_session(CLAUDE)
    assert not r["ok"] and r["error"] == "repeat ignored"
    tf._last_at = time.monotonic() - tf.REPEAT_S - 0.01
    assert tf.focus_session(CLAUDE)["ok"]


def test_a_request_while_one_runs_is_dropped(env):
    win, _, _, _ = env
    wt_window(win)
    with tf._gate:
        r = tf.focus_session(CLAUDE)
    assert r["error"] == "busy"


def test_time_budget_caps_the_whole_request(env, monkeypatch):
    monkeypatch.setattr(tf, "BUDGET_S", 0.3)
    monkeypatch.setattr(tf, "FOCUS_RESERVE_S", 0.05)
    win, uia = multi(env, ("Claude Code", "x", "Claude Code", "x"), lag=10_000)
    real = uia.tabs

    def slow(hwnd):
        time.sleep(0.03)
        return real(hwnd)
    uia.tabs = slow
    win.accept = set()                      # and the window refuses, too
    t0 = time.monotonic()
    r = tf.focus_session(CLAUDE)
    took = time.monotonic() - t0
    assert took < 0.3 + 0.15 and r["ms"] <= 450
    assert win.console[CLAUDE]["title"] == "Claude Code"   # marker still restored


def test_vscode_window_by_folder_name_and_no_tab(env):
    win, procs, box, loads = env
    procs.chain = [(CLAUDE, "claude.exe"), (SHELL, "pwsh.exe"), (70, "Code.exe"),
                   (60, "Code.exe"), (50, "explorer.exe")]
    procs._cwd = r"C:\work\homebox"
    win.consoles = [(0x10, PSEUDO, SHELL)]           # ConPTY without an owner
    win.classes[0x10] = PSEUDO
    win.tops = {60: [(0x61, 60, "plan.md - other - Visual Studio Code"),
                     (0x62, 60, "README.md - homebox - Visual Studio Code")],
                50: [(0x51, 50, "Program Manager")]}
    r = tf.focus_session(CLAUDE)
    assert r["ok"] and win.fg == 0x62 and r["tab"] == "none"
    assert loads == [] and not any(c[0] in ("attach", "swap") for c in win.calls)


def test_explorer_and_terminal_windows_are_never_guessed(env):
    win, procs, _, _ = env
    procs.chain = [(CLAUDE, "claude.exe"), (TERM, "WindowsTerminal.exe"), (50, "explorer.exe")]
    win.tops = {TERM: [(0x71, TERM, "Claude Code")], 50: [(0x51, 50, "Program Manager")]}
    r = tf.focus_session(CLAUDE)
    assert not r["ok"] and r["error"] == "no window found"


def test_never_raises_and_leaves_no_threads(env):
    win, procs, _, _ = env
    wt_window(win)
    before = set(threading.enumerate())

    def boom(pid):
        raise RuntimeError("psutil exploded")
    procs.ancestors = boom
    r = tf.focus_session(CLAUDE)
    assert not r["ok"] and "psutil exploded" in r["error"]
    assert set(threading.enumerate()) == before
    assert not tf._gate.locked()
