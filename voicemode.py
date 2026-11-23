"""Which push-to-talk the Clawdmeter's Voice key should hold.

Claude Code's own voice (hold Space) only works on Anthropic's API. Sessions
started with the ``claudez`` / ``claudek`` / ``claudeq`` aliases run on a
third-party endpoint; they get Handy instead (hold Ctrl+Space, its
"transcribe" shortcut), and so does any window that is not a Claude Code
session, since Handy dictates into whatever is in front.

How a session is recognised as third-party: those aliases start Claude Code
with ``--settings <...>\\launch-settings*.json`` (claudez:
launch-settings.json, claudek: launch-settings-claudek.json, claudeq:
launch-settings-claudeq.json), and that stays in the process's command line.
Which session is in front: Windows Terminal titles its window after the
active tab, and Claude Code titles the tab after the session ("◐ Clawdmeter").
"""
from __future__ import annotations

import os
import re

CLAUDE = "c"    # Claude Code on Anthropic: hold Space
HANDY = "h"     # Claude Code on a third-party API: hold Ctrl+Space (Handy)
OTHER = "o"     # not a Claude Code session: Handy too; the gadget clears with Ctrl+Z

_LAUNCH = re.compile(r"^launch-settings.*\.json$", re.IGNORECASE)
_GLYPH = re.compile(r"^[^\w]+", re.UNICODE)   # same as termfocus._strip


def strip_title(title: str) -> str:
    return _GLYPH.sub("", str(title or "")).strip()


def third_party_cmdline(cmdline) -> bool:
    """True when the command line carries an alias's launch-settings file."""
    args = [str(a) for a in (cmdline or [])]
    for i, a in enumerate(args):
        if a == "--settings" and i + 1 < len(args):
            value = args[i + 1]
        elif a.startswith("--settings="):
            value = a.split("=", 1)[1]
        else:
            continue
        if _LAUNCH.match(os.path.basename(value.strip().strip('"'))):
            return True
    return False


class ThirdPartyCache:
    """third_party_cmdline per process, read once per (pid, start time)."""

    def __init__(self, cmdline_of=None):
        self._cmdline_of = cmdline_of or _psutil_cmdline
        self._cache: dict = {}

    def __call__(self, session: dict) -> bool:
        pid = session.get("pid")
        if not isinstance(pid, int):
            return False
        key = (pid, session.get("started"))
        if key not in self._cache:
            if len(self._cache) > 256:
                self._cache.clear()
            try:
                self._cache[key] = third_party_cmdline(self._cmdline_of(pid))
            except Exception:
                return False          # not cached: try again next time
        return self._cache[key]


def _psutil_cmdline(pid: int):
    import psutil
    return psutil.Process(pid).cmdline()


def pick(sessions, front_title: str, is_third_party) -> str:
    """CLAUDE when the window in front is an Anthropic Claude Code session,
    HANDY when it is a third-party one (or a title several sessions share and
    they don't all agree), OTHER when it is no Claude Code session at all."""
    want = strip_title(front_title).casefold()
    if not want:
        return OTHER
    hits = [s for s in sessions or [] if str(s.get("name") or "").strip().casefold() == want]
    if not hits:
        return OTHER
    # Codex has no Space-to-talk of its own: Handy dictates into it
    return HANDY if any(s.get("tool") == "codex" or is_third_party(s) for s in hits) else CLAUDE


def front_title() -> str:
    """Title of the window in front, "" when unknown (not Windows, no window)."""
    try:
        import termfocus
        w = termfocus._win()
        h = w.foreground()
        return w.title(h) if h else ""
    except Exception:
        return ""
