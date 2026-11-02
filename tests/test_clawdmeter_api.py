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
