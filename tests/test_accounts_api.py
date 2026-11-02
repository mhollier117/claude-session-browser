"""The Accounts page's bridge (Api.accounts_*) and the tray submenu.

Uses the fake sign-in setup from test_accounts.py; the real Api is created
without its constructor so no settings, buddy or windows are touched.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
pytest.importorskip("webview")
import claude_sessions as cs  # noqa: E402
from test_accounts import UA, UB, env, two_accounts  # noqa: E402,F401


@pytest.fixture
def api(env):
    mgr, http, clock, paths = env
    two_accounts(mgr, http, clock, paths)
    a = cs.Api.__new__(cs.Api)
    a._accounts = mgr
    a._kick_usage_poll = lambda: None
    return a, paths


def test_list_switch_rename_remove(api):
    a, paths = api
    r = a.accounts_list()
    assert r["ok"] and [x["live"] for x in r["accounts"]] == [True, False]

    r = a.accounts_switch(UA)
    assert r["ok"] and r["changed"] is True
    assert [x["uuid"] for x in r["accounts"] if x["live"]] == [UA]

    r = a.accounts_rename(UB, "Personal")
    assert r["ok"] and any(x["nickname"] == "Personal" for x in r["accounts"])

    r = a.accounts_remove(UA)          # live now -> refused, as a clean error
    assert r == {"ok": False, "error": "Can't remove the account that's signed in right now."}
    assert a.accounts_remove(UB)["ok"]


def test_errors_come_back_as_messages_not_exceptions(api):
    a, _ = api
    r = a.accounts_switch("not a real id")
    assert r["ok"] is False and r["error"]


def test_add_clears_signin_and_reports_stored_account(api):
    a, paths = api
    r = a.accounts_add()
    assert r["ok"] and r["stored"] == UB
    assert not any(x["live"] for x in r["accounts"])


def test_tray_submenu_lists_accounts_and_switches(api):
    pystray = pytest.importorskip("pystray")
    a, _ = api
    switched = []
    tray = cs.TrayManager(lambda: None, lambda: None,
                          accounts=lambda: a.accounts_list()["accounts"],
                          on_switch=switched.append)
    items = tray._account_items()
    assert [i.text for i in items] == ["b", "a"]          # live first
    assert [i.checked for i in items] == [True, False]
    items[1](None)                                         # click "a"
    assert switched == [UA]
    menu = pystray.Menu(lambda: iter(tray._account_items()))
    assert len(list(menu.items)) == 2


def test_limit_card_falls_back_to_accounts_for_plans_without_5h(api, env):
    """Ultra (formerly Max 20x) never sends 5-hour headers; the "Your limit"
    card must use the account usage and say there's no 5-hour limit."""
    a, _ = api
    mgr, http, clock, paths = env
    http.usage[UB] = {"five_hour": None,
                      "seven_day": {"utilization": 12.0, "resets_at": "2027-01-20T08:00:00Z"}}
    mgr._update_meta(UB, plan="Ultra")
    mgr.poll(UB)
    a._usage_meta = None
    a._LIMIT_HEADER_TRUST_S = 10 ** 12          # fake clock is far in the future
    st = a.limit_state()
    assert st["known"] and st["no_five"] is True and st["wpct"] == 12 and st["plan"] == "Ultra"


def test_limit_card_shows_both_for_plans_with_5h(api, env):
    a, _ = api
    mgr, http, clock, paths = env
    http.usage[UB] = {"five_hour": {"utilization": 30.0, "resets_at": "2027-01-15T08:00:00Z"},
                      "seven_day": {"utilization": 50.0, "resets_at": "2027-01-20T08:00:00Z"}}
    mgr.poll(UB)
    a._usage_meta = None
    a._LIMIT_HEADER_TRUST_S = 10 ** 12
    st = a.limit_state()
    assert st["no_five"] is False and st["pct"] == 30 and st["wpct"] == 50


def test_fable_notice_once_per_period_and_quiet_when_free(monkeypatch):
    sent = []
    a = cs.Api.__new__(cs.Api)
    a.settings = {}
    a._tray_notify = sent.append
    monkeypatch.setattr(cs, "save_json", lambda *args, **kw: None)
    acct = lambda f, reset: [{"uuid": UA, "nickname": "Main",
                              "usage": {"f": f, "f_reset_at": reset}}]
    a._check_fable_limits(acct(100, 2e9))
    a._check_fable_limits(acct(100, 2e9))            # same period: no repeat
    assert len(sent) == 1 and "Main" in sent[0]
    a._check_fable_limits(acct(3, 2.6e9))            # reset: silent, re-armed
    assert len(sent) == 1
    a._check_fable_limits(acct(100, 2.6e9))          # next period: notify again
    assert len(sent) == 2
    a.settings["notify_fable_limit"] = False
    a.settings["fable_notified"] = {}
    a._check_fable_limits(acct(100, 3e9))            # switched off
    assert len(sent) == 2


# -- switch requests from the Clawdmeter (REQ 0x02) ----------------------------
# Order as sent to the device: live first, then by nickname -> [b, a] here.

@pytest.fixture
def gadget(api):
    a, paths = api
    logs, switched = [], []
    a._clawd_log = logs.append
    real = a.accounts_switch

    def spy(uuid):
        switched.append(uuid)
        return real(uuid)
    a.accounts_switch = spy
    return a, logs, switched


def live_uuid(a):
    return next(x["uuid"] for x in a.accounts_list()["accounts"] if x["live"])


def test_gadget_switch_by_index_and_name(gadget):
    a, logs, switched = gadget
    a.accounts_rename(UA, "Personal")
    a._clawd_switch_request(1, "Personal")
    assert switched == [UA] and live_uuid(a) == UA
    assert "Personal" in logs[-1]


def test_gadget_switch_finds_name_when_index_is_stale(gadget):
    a, logs, switched = gadget
    a.accounts_rename(UA, "Personal")
    a._clawd_switch_request(0, "Personal")      # index 0 is "b" (live) now
    assert switched == [UA] and live_uuid(a) == UA


def test_gadget_switch_unknown_name_does_nothing(gadget):
    a, logs, switched = gadget
    a._clawd_switch_request(1, "Nobody")
    a._clawd_switch_request(1, "")
    a._clawd_switch_request(9, "Nobody")
    assert switched == [] and live_uuid(a) == UB
    assert len(logs) == 3 and all("ignored" in m for m in logs)


def test_gadget_switch_to_live_account_does_nothing(gadget):
    a, logs, switched = gadget
    a._clawd_switch_request(0, "b")
    assert switched == [] and live_uuid(a) == UB
    assert "already active" in logs[-1]


def test_gadget_switch_matches_shortened_nickname(gadget):
    a, logs, switched = gadget
    a.accounts_rename(UA, "A very long nickname")     # sent as "A very long ni" or less
    a._clawd_switch_request(1, "A very l")
    assert switched == [UA] and live_uuid(a) == UA


def test_gadget_switch_ambiguous_prefix_does_nothing(gadget):
    a, logs, switched = gadget
    a.accounts_rename(UA, "Work one")
    a.accounts_rename(UB, "Work two")
    a._clawd_switch_request(5, "Work")                 # bad index, two prefix hits
    assert switched == [] and "ambiguous" in logs[-1]
    a._clawd_switch_request(1, "Work")                 # index agrees: take it
    assert switched == [UA] and live_uuid(a) == UA
