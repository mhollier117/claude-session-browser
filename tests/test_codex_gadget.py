"""Codex usage and sessions beside Claude's on the Clawdmeter and in the app.

Stub-app pattern of test_clawdmeter_api.py: the Api is built without its
constructor; no real ~/.codex, accounts file or Bluetooth is touched.
"""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
pytest.importorskip("webview")
import claude_sessions as cs  # noqa: E402
import sessions_live as sl  # noqa: E402

CODEX = {"s": None, "s_reset_at": 0.0, "w": 12, "w_reset_at": time.time() + 3 * 86400,
         "limited": False, "plan": "plus", "at": time.time()}


class FakeAccounts:
    def __init__(self, accts):
        self._a = accts

    def accounts(self):
        return self._a


class FakeSvc:
    def __init__(self, sess):
        self._s = sess

    def latest(self):
        return self._s


def acct(nick, live=False):
    return {"nickname": nick, "live": live, "has_room": True, "uuid": "u-" + nick,
            "usage": {"s": 30, "s_reset_at": time.time() + 600, "w": 40,
                      "w_reset_at": time.time() + 86400, "f": None}}


def sess(name, tool="claude", sid=None):
    return {"name": name, "state": sl.WORKING, "since": None, "started": 1.0,
            "session_id": sid or name, "tool": tool, "pid": 1, "cwd": "C:/x"}


def app_with(accts=(), sessions=(), codex=CODEX):
    a = cs.Api.__new__(cs.Api)
    a.settings = {}
    a._accounts = FakeAccounts(list(accts))
    a._sessions_service = FakeSvc(list(sessions))
    a._codex_usage_cached = lambda: codex
    a._logs = []
    a._clawd_log = a._logs.append
    return a


def test_codex_row_after_claude_accounts_live_when_claude_live():
    ac = app_with([acct("A", live=True), acct("B")])._clawd_extras()["ac"]
    assert len(ac) == 3
    row = ac[2]
    assert row["n"] == "Codex" and row["l"] == 1 and row["r"] == 1
    assert row["s"] == -1 and row["w"] == 12 and row["f"] == -1 and row["fr"] == 0
    assert row["sr"] == 0 and row["wr"] > 0
    assert [r["n"] for r in ac[:2]] == ["A", "B"] and ac[0]["l"] == 1 and ac[1]["l"] == 0


def test_codex_row_not_live_without_live_claude_account():
    ac = app_with([])._clawd_extras()["ac"]
    assert ac[0]["n"] == "Codex" and ac[0]["l"] == 0
    ac = app_with([acct("A"), acct("B")])._clawd_extras()["ac"]
    assert ac[2]["n"] == "Codex" and ac[2]["l"] == 0 and ac[0]["l"] == 0


def test_claude_accounts_capped_at_five_when_codex_present():
    ac = app_with([acct(f"A{i}", live=(i == 0)) for i in range(6)])._clawd_extras()["ac"]
    assert len(ac) == 6 and ac[5]["n"] == "Codex"
    assert [r["n"] for r in ac[:5]] == [f"A{i}" for i in range(5)]


def test_six_claude_accounts_without_codex_unchanged():
    ac = app_with([acct(f"A{i}") for i in range(6)], codex=None)._clawd_extras()["ac"]
    assert len(ac) == 6 and all(r["n"] != "Codex" for r in ac)


def test_no_codex_row_without_usage():
    a = app_with([acct("A", live=True)], codex=None)
    assert [r["n"] for r in a._clawd_extras()["ac"]] == ["A"]
    assert a._clawd_codex_index is None


def test_codex_limited_row_has_no_room_and_short_window():
    u = dict(CODEX, s=100, s_reset_at=time.time() + 1800, limited=True)
    row = app_with([acct("A", live=True)], codex=u)._clawd_extras()["ac"][1]
    assert row["r"] == 0 and row["s"] == 100 and row["sr"] > 0


def test_codex_index_recorded():
    a = app_with([acct("A", live=True), acct("B")])
    a._clawd_extras()
    assert a._clawd_codex_index == 2
    a2 = app_with([acct("A")], codex=None)
    a2._clawd_extras()
    assert a2._clawd_codex_index is None


def test_codex_sessions_prefixed_cx_on_gadget():
    a = app_with([acct("A", live=True)],
                 [sess("api", "claude"), sess("tool", "codex")])
    names = [s["n"] for s in a._clawd_extras()["se"]]
    assert sorted(names) == ["api", "cx tool"]
    gn = [s["gname"] for s in a._clawd_gadget_sessions()]
    assert "cx tool" in gn
    s, why = a._clawd_resolve_session(None, "cx tool")
    assert s is not None and s["tool"] == "codex"


def test_switch_request_to_codex_row_ignored():
    a = app_with([acct("A", live=True), acct("B")])
    a._clawd_extras()
    called = []
    a.accounts_switch = lambda uuid: called.append(uuid) or {"ok": True}
    a._clawd_switch_request(a._clawd_codex_index, "Codex")
    assert called == []
    assert any("ignored" in m and "Codex" in m for m in a._logs)


def test_switch_request_to_claude_account_still_works():
    a = app_with([acct("A", live=True), acct("B")])
    a._clawd_extras()
    called = []
    a.accounts_switch = lambda uuid: called.append(uuid) or {"ok": True, "changed": True}
    a._clawd_switch_request(1, "B")
    assert called == ["u-B"]


def test_main_usage_payload_stays_claude():
    a = app_with([acct("A", live=True)])
    a._LIMIT_HEADER_TRUST_S = 600
    live = acct("A", live=True)
    live["usage_age_s"] = 5
    a._accounts = FakeAccounts([live])
    assert a._clawd_usage() == live["usage"]


def test_limit_state_has_codex_block():
    a = app_with([acct("A", live=True)])
    a._LIMIT_HEADER_TRUST_S = 600
    a._usage_meta = {"pct": 10, "reset_at": time.time() + 100, "at": time.time(),
                     "wpct": 20, "wreset_at": time.time() + 1000}
    st = a.limit_state()
    assert st["pct"] == 10 and st["wpct"] == 20
    c = st["codex"]
    assert c["spct"] is None and c["wpct"] == 12 and c["hit"] is False and c["plan"] == "plus"
    assert c["wreset_at"] > 0 and c["sreset_at"] == 0


def test_limit_state_codex_none_without_usage():
    a = app_with([acct("A", live=True)], codex=None)
    a._LIMIT_HEADER_TRUST_S = 600
    a._usage_meta = {"pct": 10, "at": time.time()}
    assert a.limit_state()["codex"] is None


def test_codex_usage_cached_30s(monkeypatch):
    import codex_store
    calls = []
    monkeypatch.setattr(codex_store, "usage", lambda home, now: calls.append(now) or {"w": 1})
    monkeypatch.setattr(codex_store, "codex_home", lambda: Path("nowhere"))
    a = cs.Api.__new__(cs.Api)
    assert a._codex_usage_cached() == {"w": 1}
    assert a._codex_usage_cached() == {"w": 1}
    assert len(calls) == 1
    a._codex_usage_cache_at -= 31
    a._codex_usage_cached()
    assert len(calls) == 2


def test_codex_usage_cached_survives_errors(monkeypatch):
    import codex_store
    monkeypatch.setattr(codex_store, "codex_home", lambda: Path("nowhere"))
    monkeypatch.setattr(codex_store, "usage", lambda h, n: (_ for _ in ()).throw(OSError("x")))
    assert cs.Api.__new__(cs.Api)._codex_usage_cached() is None
