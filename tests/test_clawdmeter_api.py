"""The Clawdmeter bridge for several PCs: Api.clawdmeter_show_here and the
tray item "Show on the Clawdmeter".

The real Api is created without its constructor, with a fake link, so no
settings file, Bluetooth or windows are touched.
"""

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
pytest.importorskip("webview")
import claude_sessions as cs  # noqa: E402


class FakeLink:
    def __init__(self, sent=True, shown=None):
        self.calls = 0
        self.sent = sent
        self.shown = shown

    def request_show(self):
        self.calls += 1
        return self.sent

    def is_running(self):
        return True

    def status(self):
        return {"connected": True, "shown": self.shown}


def api_with(link, on=True):
    a = cs.Api.__new__(cs.Api)
    a.settings = {"clawdmeter": on}
    a._clawdmeter = link
    return a


def test_show_here_asks_the_link_and_returns_the_state():
    link = FakeLink(shown=False)
    r = api_with(link).clawdmeter_show_here()
    assert link.calls == 1 and r["sent"] is True
    assert r["enabled"] and r["available"] and r["status"]["shown"] is False


def test_show_here_reports_a_request_that_did_not_go_out():
    r = api_with(FakeLink(sent=False)).clawdmeter_show_here()
    assert r["sent"] is False and r["status"]["shown"] is None


def test_show_here_does_nothing_while_the_link_is_off():
    link = FakeLink()
    r = api_with(link, on=False).clawdmeter_show_here()
    assert link.calls == 0 and r["sent"] is False and r["enabled"] is False


def test_show_here_survives_a_failing_link():
    class Boom(FakeLink):
        def request_show(self):
            raise RuntimeError("nope")
    assert api_with(Boom()).clawdmeter_show_here()["sent"] is False


def test_tray_item_only_while_the_link_is_on_and_sends_off_the_menu_thread():
    pystray = pytest.importorskip("pystray")
    on, done, threads = {"v": True}, threading.Event(), []

    def show():
        threads.append(threading.current_thread().name)
        done.set()

    tray = cs.TrayManager(lambda: None, lambda: None,
                          clawd_enabled=lambda: on["v"], clawd_show=show)
    item = tray._clawd_item(pystray)
    assert item.text in ("Show on the Clawdmeter", "Auf dem Clawdmeter zeigen")
    assert item.visible is True
    on["v"] = False
    assert item.visible is False
    item(None)                                             # click
    assert done.wait(2) and threads == ["clawdmeter-show"]


# -- focus requests from the Clawdmeter (REQ 0x05) -----------------------------

class FakeSessions:
    def __init__(self, sessions):
        self.sessions = sessions

    def latest(self):
        return self.sessions


def sess(name, pid):
    return {"name": name, "pid": pid, "cwd": rf"C:\work\{pid}", "state": "idle"}


@pytest.fixture
def focus_api(monkeypatch):
    import termfocus
    a = cs.Api.__new__(cs.Api)
    a.settings = {}
    a._sessions_service = FakeSessions([
        sess("Clawdmeter", 11), sess("better orca", 22), sess("better_orca", 33),
        sess("Names for Casper's cleaning service", 44), sess("✳ Ünïcode", 55)])
    logs, focused = [], []
    a._clawd_log = logs.append

    def fake_focus(pid, cwd=None):
        focused.append((pid, cwd))
        return {"ok": True, "window": True, "tab": "exact", "via": "plain", "ms": 7,
                "error": "", "note": ""}
    monkeypatch.setattr(termfocus, "focus_session", fake_focus)
    return a, logs, focused


def test_gadget_focus_by_index_and_name(focus_api):
    a, logs, focused = focus_api
    a._clawd_focus_request(1, "better orca")
    assert focused == [(22, r"C:\work\22")]
    assert len(logs) == 1 and "better orca" in logs[0] and "tab=exact" in logs[0]
    assert "7 ms" in logs[0]


def test_gadget_focus_shortened_name_at_its_index(focus_api):
    a, _, focused = focus_api
    # Sent as "Names for Casper's c" (SE_NAME_MAX) or shorter for a small MTU.
    a._clawd_focus_request(3, "Names for")
    assert focused == [(44, r"C:\work\44")]


def test_gadget_focus_finds_the_name_when_the_index_is_stale(focus_api):
    a, _, focused = focus_api
    a._clawd_focus_request(0, "better_orca")        # index 0 is "Clawdmeter" now
    assert focused[0][0] == 33
    a._clawd_focus_request(9, "ncode")              # out of range; "✳ Ünïcode" as sent (ASCII only)
    assert focused[1][0] == 55


def test_gadget_focus_ambiguous_name_does_nothing(focus_api):
    a, logs, focused = focus_api
    a._clawd_focus_request(0, "better")             # prefix of two sessions
    assert focused == [] and "ambiguous" in logs[-1]
    a._clawd_focus_request(2, "better")             # index agrees: take it
    assert focused == [(33, r"C:\work\33")]


def test_gadget_focus_unknown_or_empty_name_does_nothing(focus_api):
    a, logs, focused = focus_api
    a._clawd_focus_request(1, "Nobody")
    a._clawd_focus_request(1, "")
    a._sessions_service = None                      # no session list at all
    a._clawd_focus_request(0, "Clawdmeter")
    assert focused == []
    assert len(logs) == 3 and all("ignored" in m for m in logs)
    assert "not found" in logs[0] and "no name" in logs[1]


def test_gadget_focus_logs_a_failure_in_one_line(focus_api, monkeypatch):
    import termfocus
    a, logs, _ = focus_api
    monkeypatch.setattr(termfocus, "focus_session", lambda pid, cwd=None: {
        "ok": False, "window": False, "tab": "none", "via": "", "ms": 1500,
        "error": "window did not come to the front", "note": "budget"})
    a._clawd_focus_request(0, "Clawdmeter")
    assert len(logs) == 1 and "failed" in logs[0] and "did not come" in logs[0]


def test_link_gets_the_focus_callback(monkeypatch):
    a = cs.Api.__new__(cs.Api)
    a.settings = {}
    link = a._clawd_link()
    assert link is not None and link._on_focus_request == a._clawd_focus_request
