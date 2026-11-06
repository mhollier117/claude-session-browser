"""The Clawdmeter Bluetooth messages built on the PC (protocol 1 + 2)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import clawdmeter as cm  # noqa: E402

NOW = 1_800_000_000.0


def test_usage_payload_for_a_plan_without_5h_limit_and_with_fable():
    u = {"s": None, "s_reset_at": 0, "w": 12, "w_reset_at": NOW + 3600,
         "f": 4, "f_reset_at": NOW + 7200, "limited": False, "at": NOW}
    p, meta = cm.payload_from_usage(u, NOW)
    assert p["x5"] == 1 and p["s"] == 0 and p["w"] == 12 and p["wr"] == 60
    assert p["f"] == 4 and p["fr"] == 120 and p["st"] == "allowed"
    assert meta["weekly_pct"] == 12


def test_usage_payload_with_5h_limit_and_no_fable():
    u = {"s": 100, "s_reset_at": NOW + 600, "w": 50, "w_reset_at": NOW + 60,
         "f": None, "limited": True, "at": NOW}
    p, _ = cm.payload_from_usage(u, NOW)
    assert "x5" not in p and "f" not in p and p["s"] == 100 and p["sr"] == 10
    assert p["st"] == "rejected"


def link(extras, mtu=185, proto=2):
    lk = cm.ClawdmeterLink(extras_provider=lambda: extras)
    lk._mtu, lk._proto = mtu, proto
    return lk


def test_extras_fit_one_packet_and_trim():
    long = "A session name that is far too long for the little screen"
    ex = {"se": [{"n": long, "a": "work coding", "s": "w", "c": 100, "w": "200K"}
                 for _ in range(10)],
          "ac": [{"n": "an-extremely-long-nickname", "l": 1, "r": 1, "s": 9, "sr": 180,
                  "w": 32, "wr": 4320, "f": 4, "fr": 5000}]}
    msgs = link(ex, mtu=90)._extras_messages()
    for key, text in msgs.items():
        if key.startswith("se"):
            assert len(text.encode()) <= 87
    assert "ss" not in msgs
    assert [k for k in msgs if k.startswith("se")] == [f"se{i}" for i in range(cm.SE_MAX)]
    for i in range(cm.SE_MAX):
        se = json.loads(msgs[f"se{i}"])
        assert se["k"] == "se" and se["i"] == i and se["t"] == cm.SE_MAX
        assert cm.SE_NAME_MIN <= len(se["n"]) < cm.SE_NAME_MAX and long.startswith(se["n"])
        assert se["s"] == "w" and se["c"] == 100 and se["w"] == "200K"
    msgs = link(ex, mtu=120)._extras_messages()
    for text in msgs.values():
        assert len(text.encode()) <= 117
    ac = json.loads(msgs["ac0"])
    assert ac["k"] == "ac" and ac["i"] == 0 and ac["c"] == 1 and len(ac["n"]) <= 14


def test_extras_are_ascii_only():
    msgs = link({"se": [{"n": "Café ✳ naïve", "a": "done", "s": "d"}], "ac": []})._extras_messages()
    assert json.loads(msgs["se0"])["n"] == "Caf nave"


def test_no_open_sessions_sends_one_empty_message():
    for ex in ({"se": [], "ac": []}, {}, {"se": None}):
        msgs = link(ex)._extras_messages()
        assert msgs == {"se": '{"k":"se","t":0}'}


class FakeClient:
    def __init__(self):
        self.writes = []

    async def write_gatt_char(self, _uuid, data, response=False):
        self.writes.append(json.loads(bytes(data)))


def sent_payload(session):
    import asyncio
    lk = cm.ClawdmeterLink(session_provider=lambda: session)
    lk._last_payload = {"s": 1, "w": 2}           # skip the usage poll
    client = FakeClient()
    asyncio.run(lk._send_once(client, poll=False))
    return lk, client.writes[0]


def test_usage_payload_carries_context_of_the_named_session():
    _, p = sent_payload({"n": "Fix login", "h": "", "cx": 33, "cw": "1M"})
    assert p["n"] == "Fix login" and p["cx"] == 33 and p["cw"] == "1M"


def test_usage_payload_omits_context_when_unknown():
    for session in ({"n": "Fresh", "h": ""}, {"n": "Fresh", "h": "", "cx": None, "cw": ""},
                    {"n": "Fresh", "h": "", "cx": 5}):            # no window: unusable
        _, p = sent_payload(session)
        assert "cx" not in p and "cw" not in p


def test_context_change_triggers_resend_only_on_whole_percent():
    state = {"n": "A", "h": "", "cx": 33, "cw": "1M"}
    lk, _ = sent_payload(state)
    lk._session_provider = lambda: state
    assert not lk._anim_changed()
    state["cx"] = 33.4                               # provider gives ints; still no spam
    assert not lk._anim_changed()
    state["cx"] = 34
    assert lk._anim_changed()
    state.update(cx=34, cw="200K")
    assert lk._anim_changed()


def test_session_entries_carry_state_and_context():
    ex = {"se": [{"n": "Fix login", "a": "allow", "s": "n", "c": 33, "w": "1M"},
                 {"n": "Fresh", "a": "work coding", "s": "w"}], "ac": []}
    msgs = link(ex, mtu=247)._extras_messages()
    assert set(msgs) == {"se0", "se1"}
    for i, want in enumerate(ex["se"]):
        se = json.loads(msgs[f"se{i}"])
        assert se == {"k": "se", "i": i, "t": 2, **want}
    assert "c" not in json.loads(msgs["se1"]) and "w" not in json.loads(msgs["se1"])


def test_long_names_are_shortened_per_message_but_not_below_minimum():
    names = ["Refactor the payment service", "Fix flaky login test suite", "Short"]
    ex = {"se": [{"n": n, "a": "work coding", "s": "w", "c": 100, "w": "200K"} for n in names]}

    def se_for(mtu):
        msgs = link(ex, mtu=mtu)._extras_messages()
        out = []
        for i in range(len(names)):
            text = msgs[f"se{i}"]
            n = json.loads(text)["n"]
            assert len(text.encode()) <= mtu - 3 or len(n[:-1].rstrip()) < cm.SE_NAME_MIN
            out.append(json.loads(text))
        return out

    roomy = se_for(247)
    assert [x["n"] for x in roomy] == [cm._ascii(n, cm.SE_NAME_MAX).rstrip() for n in names]
    # Tight: only the names that do not fit get cut, each message on its own.
    full = len(json.dumps(roomy[0], separators=(",", ":")))
    tight = se_for(full - 5 + 3)
    assert all(len(json.dumps(x, separators=(",", ":"))) <= full - 5 for x in tight)
    assert len(tight[0]["n"]) < len(roomy[0]["n"]) and tight[2]["n"] == "Short"
    assert all(n.startswith(x["n"]) for n, x in zip(names, tight))
    # Far too small: names stop at the minimum, nothing is dropped.
    # A cut that would end on a space and leave fewer than four is skipped.
    tiny = se_for(23)
    assert [x["n"] for x in tiny] == ["Refa", "Fix f", "Shor"]
    assert [x["i"] for x in tiny] == [0, 1, 2] and all(x["t"] == 3 for x in tiny)


def sent_extras(lk):
    import asyncio
    client = FakeClient()
    asyncio.run(lk._send_extras(client))
    return client.writes


def test_only_changed_session_messages_are_resent_and_t_trims():
    rows = [{"n": f"Session {i}", "a": "work coding", "s": "w"} for i in range(5)]
    ex = {"se": rows, "ac": []}
    lk = link(ex)
    lk._last_payload = {"s": 1, "w": 2}
    first = sent_extras(lk)
    assert [(m["i"], m["t"]) for m in first] == [(i, 5) for i in range(5)]
    assert sent_extras(lk) == [] and not lk._anim_changed()       # nothing new
    rows[3]["s"] = "n"                                          # one session changes
    assert lk._anim_changed()
    assert [(m["i"], m["s"]) for m in sent_extras(lk)] == [(3, "n")]
    # Fewer sessions: every remaining message carries the new t, so all go out
    # and the device trims its list; the dropped keys are forgotten.
    del rows[2:]
    assert lk._anim_changed()
    assert [(m["i"], m["t"]) for m in sent_extras(lk)] == [(0, 2), (1, 2)]
    assert set(lk._last_extras) == {"se0", "se1"}
    # Grows again: the old keys se2..se4 must not suppress anything.
    rows.extend({"n": f"Session {i}", "a": "work coding", "s": "w"} for i in range(2, 5))
    assert [(m["i"], m["t"]) for m in sent_extras(lk)] == [(i, 5) for i in range(5)]
    # None left: one empty message, then silence; and back from empty.
    del rows[:]
    assert sent_extras(lk) == [{"k": "se", "t": 0}]
    assert sent_extras(lk) == [] and not lk._anim_changed()
    rows.append({"n": "Back", "a": "done", "s": "d"})
    assert lk._anim_changed()
    assert sent_extras(lk) == [{"k": "se", "i": 0, "t": 1, "n": "Back", "a": "done", "s": "d"}]
    rows.clear()
    assert sent_extras(lk) == [{"k": "se", "t": 0}]


# -- sessions keep flowing when the usage poll fails --------------------------

def _failing_poll(monkeypatch, *, token):
    """No usage numbers: either no token at all, or the API call fails."""
    monkeypatch.setattr(cm, "read_token", lambda: token)
    monkeypatch.setattr(cm, "poll_usage_meta", lambda _tok: (None, {}))


def _send_with_failed_poll(lk):
    import asyncio
    client = FakeClient()
    asyncio.run(lk._send_once(client, poll=True))
    return client.writes


def test_sessions_still_sent_when_there_is_no_token(monkeypatch):
    _failing_poll(monkeypatch, token=None)
    lk = link({"se": [{"n": "Fix login", "a": "allow", "s": "n"}]})
    writes = _send_with_failed_poll(lk)
    assert writes == [{"k": "se", "i": 0, "t": 1, "n": "Fix login", "a": "allow", "s": "n"}]
    assert lk.status()["last_error"]                       # the error is still shown
    assert lk._last_payload is None                        # no usage numbers invented


def test_sessions_still_sent_when_the_api_call_fails(monkeypatch):
    _failing_poll(monkeypatch, token="tok")
    rows = [{"n": "A", "a": "work coding", "s": "w"}]
    lk = link({"se": rows})
    assert [m["n"] for m in _send_with_failed_poll(lk)] == ["A"]
    # The next failed poll only sends what changed.
    assert _send_with_failed_poll(lk) == []
    rows[0]["s"] = "n"
    assert [(m["n"], m["s"]) for m in _send_with_failed_poll(lk)] == [("A", "n")]


def test_failed_poll_sends_nothing_to_old_firmware_or_when_another_pc_is_shown(monkeypatch):
    _failing_poll(monkeypatch, token=None)
    ex = {"se": [{"n": "A", "a": "done", "s": "d"}]}
    assert _send_with_failed_poll(link(ex, proto=1)) == []
    lk = link(ex)
    lk._set(shown=False)
    assert _send_with_failed_poll(lk) == []


def test_session_changes_are_noticed_before_any_usage_numbers_arrived():
    rows = [{"n": "A", "a": "work coding", "s": "w"}]
    lk = link({"se": rows})
    assert lk._last_payload is None
    assert not lk._anim_changed()                  # usage path: nothing to add to
    assert lk._extras_changed()                    # sessions path: first list due
    sent_extras(lk)
    assert not lk._extras_changed()
    rows[0]["s"] = "n"
    assert lk._extras_changed()
    assert not link({"se": rows}, proto=1)._extras_changed()
    inactive = link({"se": rows})
    inactive._set(shown=False)
    assert not inactive._extras_changed()


# -- a stale Windows service cache after the gadget restarts ------------------

def test_missing_service_retries_at_once_with_a_fresh_service_list(monkeypatch):
    monkeypatch.setattr(cm.sys, "platform", "win32")
    logs = []
    lk = cm.ClawdmeterLink(log=logs.append)
    assert lk._client_options() == {}                       # normally: Windows' cache
    # First miss: most likely the cache. Retry soon, uncached, no error shown.
    assert lk._service_missing("AA:BB") <= 2
    assert lk.status()["last_error"] is None
    assert lk._client_options() == {"winrt": {"use_cached_services": False}}
    # Still missing when read fresh: a real problem, shown, normal back-off.
    assert lk._service_missing("AA:BB") == cm.RETRY_INTERVAL
    assert "Clawdmeter" in (lk.status()["last_error"] or "")
    assert lk._client_options() == {"winrt": {"use_cached_services": False}}


def test_fresh_service_list_only_on_windows(monkeypatch):
    monkeypatch.setattr(cm.sys, "platform", "linux")
    lk = cm.ClawdmeterLink()
    lk._service_missing("AA:BB")
    assert lk._client_options() == {}


# -- requests from the device on REQ (0x01 refresh, 0x02 switch account) ------

def test_parse_request():
    assert cm.parse_request(b"\x01") == ("refresh",)
    assert cm.parse_request(b"") == ("refresh",)
    assert cm.parse_request(None) == ("refresh",)
    assert cm.parse_request(b"\x07junk") == ("refresh",)
    assert cm.parse_request(b"\x02\x01work") == ("switch", 1, "work")
    assert cm.parse_request(bytearray(b"\x02\x00Main acct\x00\x00")) == ("switch", 0, "Main acct")
    assert cm.parse_request(b"\x02\x03") == ("switch", 3, "")
    assert cm.parse_request(b"\x02") is None              # no index: dropped


def handle(data, on_switch=None):
    """Run _handle_request; return (switch calls, refresh count) once the
    switch thread (if any) has finished."""
    import threading
    calls, woke, done = [], [], threading.Event()

    def cb(i, n):
        calls.append((i, n, threading.current_thread().name))

    def wake():
        woke.append(1)
        done.set()

    lk = cm.ClawdmeterLink(on_switch_request=cb if on_switch is None else on_switch)
    lk._handle_request(data, wake)
    if len(data) > 1 and data[0] == 2:
        done.wait(2)                                    # switch runs in a thread
    return calls, len(woke)


def test_switch_request_runs_callback_off_the_loop_then_refreshes():
    calls, woke = handle(b"\x02\x02Personal")
    assert [(i, n) for i, n, _ in calls] == [(2, "Personal")]
    assert calls[0][2] == "clawdmeter-switch"            # its own thread
    assert woke == 1                                     # fresh data after the swap


def test_refresh_requests_only_refresh():
    for data in (b"\x01", b"", bytearray()):
        calls, woke = handle(data)
        assert calls == [] and woke == 1


def test_malformed_switch_request_is_ignored():
    calls, woke = handle(b"\x02")
    assert calls == [] and woke == 0


def test_failing_switch_callback_still_refreshes():
    def boom(_i, _n):
        raise RuntimeError("nope")
    calls, woke = handle(b"\x02\x00a", on_switch=boom)
    assert woke == 1


def test_parse_focus_request():
    assert cm.parse_request(b"\x05\x02better orca") == ("focus", 2, "better orca")
    assert cm.parse_request(bytearray(b"\x05\x00Clawd\x00\x00junk")) == ("focus", 0, "Clawd")
    assert cm.parse_request(b"\x05\x07") == ("focus", 7, "")
    assert cm.parse_request(b"\x05\x01 caf\xc3\xa9 ") == ("focus", 1, "caf")   # ASCII only
    assert cm.parse_request(b"\x05") == ("focus", None, "")         # no index


def focus(data, cb=None):
    """Run _handle_request for a focus message: (calls, wake count)."""
    import threading
    calls, woke, done = [], [], threading.Event()

    def default(i, n):
        calls.append((i, n, threading.current_thread().name))
        done.set()

    logs = []
    lk = cm.ClawdmeterLink(log=logs.append, on_focus_request=cb or default)
    lk._handle_request(data, lambda: woke.append(1))
    done.wait(2)
    return calls, len(woke), logs


def test_focus_request_runs_callback_off_the_loop_without_refresh():
    calls, woke, _ = focus(b"\x05\x03Names for Casper")
    assert [(i, n) for i, n, _ in calls] == [(3, "Names for Casper")]
    assert calls[0][2] == "clawdmeter-focus"
    assert woke == 0                                     # nothing to resend


def test_malformed_focus_request_is_dropped():
    calls, woke, logs = focus(b"\x05")
    assert calls == [] and woke == 0 and "dropped" in logs[-1]


def test_focus_request_without_callback_does_nothing():
    lk = cm.ClawdmeterLink()
    woke = []
    lk._handle_request(b"\x05\x00a", lambda: woke.append(1))
    assert woke == []


def test_failing_focus_callback_is_logged():
    import threading
    done = threading.Event()
    logs = []

    def boom(_i, _n):
        raise RuntimeError("nope")

    def log(msg):
        logs.append(msg)
        done.set()
    lk = cm.ClawdmeterLink(log=log, on_focus_request=boom)
    lk._handle_request(b"\x05\x00a", lambda: None)
    done.wait(2)
    assert "focus request failed: nope" in logs[-1]


def test_switch_request_without_callback_is_a_refresh():
    lk = cm.ClawdmeterLink()
    woke = []
    lk._handle_request(b"\x02\x00a", lambda: woke.append(1))
    assert woke == [1]


# -- PC system stats ("sy", protocol 2) ------------------------------------

SY = {"c": 12, "cc": [10, 14], "m": 9000, "mt": 32000, "dn": "C:", "du": 400,
      "dt": 931, "dr": 5, "dw": 7, "nd": 3, "nu": 1}


def sy_link(provider, proto=2, mtu=247):
    lk = cm.ClawdmeterLink(system_provider=provider)
    lk._proto, lk._mtu = proto, mtu
    return lk


def sent_system(lk):
    import asyncio
    client = FakeClient()
    asyncio.run(lk._maybe_send_system(client))
    return client.writes


def test_system_stats_go_out_only_from_protocol_2():
    assert sent_system(sy_link(lambda: SY, proto=1)) == []
    assert sent_system(cm.ClawdmeterLink()) == []                # no provider
    lk = sy_link(lambda: SY)
    assert sent_system(lk) == [{"k": "sy", **SY}]
    text = lk._system_message().decode()
    assert text.startswith('{"k":"sy","c":12,"cc":[10,14],') and " " not in text


def test_system_stats_every_interval_regardless_of_changes(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(cm.time, "monotonic", lambda: now[0])
    lk = sy_link(lambda: SY)
    assert len(sent_system(lk)) == 1
    now[0] += 1.0                                                # 1 s tick: not yet
    assert sent_system(lk) == []
    now[0] += 1.0                                                # same values, still sent
    assert sent_system(lk) == [{"k": "sy", **SY}]
    now[0] += 1.9                                                # tick a little early
    assert len(sent_system(lk)) == 1


def test_failing_system_provider_never_breaks_the_link():
    import asyncio
    calls = {"n": 0}

    def provider():
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("psutil exploded")
        if calls["n"] == 2:
            return {"cc": "not a list", "c": object()}           # not encodable
        return SY

    logs = []
    lk = cm.ClawdmeterLink(system_provider=provider, session_provider=lambda: {"n": "", "h": ""},
                           log=logs.append)
    lk._proto, lk._mtu = 2, 247
    lk._last_payload = {"s": 1, "w": 2}
    client = FakeClient()
    asyncio.run(lk._maybe_send_system(client))
    asyncio.run(lk._maybe_send_system(client))
    assert client.writes == []
    asyncio.run(lk._send_once(client, poll=False))              # usage still goes out
    assert client.writes[0]["s"] == 1
    asyncio.run(lk._maybe_send_system(client))                  # and sy recovers
    assert client.writes[-1] == {"k": "sy", **SY}
    assert sum("system stats failed" in m for m in logs) == 1   # logged once, not per tick


def test_system_message_shrinks_only_cc_to_fit_mtu():
    stats = dict(SY, cc=[100] * 24, g=99, gm=12000, gt=24576)
    lk = sy_link(lambda: stats, mtu=247)
    full = lk._system_message()
    assert len(json.loads(full)["cc"]) == 24
    base = len(json.dumps({"k": "sy", **dict(stats, cc=[])}, separators=(",", ":")))
    for mtu in (len(full) + 2, base + 3 + 4 * 6, base + 3 + 4):
        msg_bytes = sy_link(lambda: stats, mtu=mtu)._system_message()
        assert msg_bytes is not None and len(msg_bytes) <= mtu - 3
        msg = json.loads(msg_bytes)
        assert {k: v for k, v in msg.items() if k != "cc"} == \
            {"k": "sy", **{k: v for k, v in stats.items() if k != "cc"}}
        assert msg["cc"] and all(v == 100 for v in msg["cc"])
    assert len(json.loads(sy_link(lambda: stats, mtu=len(full) + 2)._system_message())["cc"]) == 12
    # Not even one core value fits: skip this round rather than drop fields.
    assert sy_link(lambda: stats, mtu=base)._system_message() is None


def test_halve_averages_neighbours():
    assert cm._halve([0, 100, 50, 51, 7]) == [50, 51, 7]
    assert cm._halve([]) == []


# -- several PCs on one gadget: "hi" / "use", REQ 0x03 / 0x04 ----------------

USAGE = {"s": 10, "s_reset_at": 0, "w": 20, "w_reset_at": 0, "limited": False}


def multi_extras():
    return {"se": [{"n": "Fix login", "a": "work coding", "s": "w"}],
            "ac": [{"n": "Main", "l": 1}]}


def multi_link(on_usage=None, proto=2, mtu=247, extras=None):
    ex = extras if extras is not None else multi_extras()
    lk = cm.ClawdmeterLink(session_provider=lambda: {"n": "", "h": ""},
                           usage_provider=lambda: USAGE,
                           extras_provider=lambda: ex,
                           system_provider=lambda: SY, on_usage=on_usage)
    lk._proto, lk._mtu = proto, mtu
    return lk


def kinds(writes):
    return [m.get("k", "usage") for m in writes]


def cycle(lk):
    import asyncio
    client = FakeClient()
    asyncio.run(lk._cycle(client))
    return client.writes


def req(lk, data):
    """Feed one REQ notification; return how often it woke the loop."""
    woke = []
    lk._handle_request(data, lambda: woke.append(1))
    return len(woke)


def test_parse_request_shown_states():
    assert cm.parse_request(b"\x03") == ("active",)
    assert cm.parse_request(b"\x04") == ("inactive",)
    assert cm.parse_request(bytearray(b"\x03\x00")) == ("active",)
    assert cm.parse_request(b"\x01") == ("refresh",)            # unchanged


def test_status_shown_is_unknown_until_the_gadget_says():
    lk = multi_link()
    assert lk.status()["shown"] is None and lk._active()      # older firmware: active
    assert req(lk, b"\x04") == 0 and lk.status()["shown"] is False and not lk._active()
    assert req(lk, b"\x03") == 1 and lk.status()["shown"] is True and lk._active()
    assert req(lk, b"\x03") == 0                               # already shown: no resend


def test_without_shown_messages_everything_goes_out_as_before():
    assert kinds(cycle(multi_link())) == ["usage", "se", "ac", "sy"]


def test_inactive_sends_nothing_but_hi(monkeypatch):
    monkeypatch.setenv("COMPUTERNAME", "DESK-PC")
    metas, ex = [], multi_extras()
    lk = multi_link(on_usage=metas.append, extras=ex)
    assert kinds(cycle(lk)) == ["usage", "se", "ac", "sy"]
    assert req(lk, b"\x04") == 0
    assert cycle(lk) == []                                     # no usage, se/ac, sy
    assert len(metas) == 2                                     # still polled, for the limits
    ex["se"][0]["s"] = "n"                                     # a change: still silent
    assert not lk._anim_changed()
    assert sent_system(lk) == []
    assert req(lk, b"\x01") == 1                               # refresh while inactive
    assert cycle(lk) == [{"k": "hi", "n": "DESK-PC"}]          # only hi


def test_refresh_resends_everything_hi_first(monkeypatch):
    monkeypatch.setenv("COMPUTERNAME", "DESK-PC")
    lk = multi_link()
    assert kinds(cycle(lk)) == ["usage", "se", "ac", "sy"]
    assert kinds(cycle(lk)) == ["usage"]                       # extras unchanged, sy not due
    assert req(lk, b"\x01") == 1
    writes = cycle(lk)
    assert kinds(writes) == ["hi", "usage", "se", "ac", "sy"]  # nothing skipped
    assert writes[0] == {"k": "hi", "n": "DESK-PC"}
    assert kinds(cycle(lk)) == ["usage"]                       # back to change-only


def test_becoming_active_again_resends_everything(monkeypatch):
    monkeypatch.setenv("COMPUTERNAME", "DESK-PC")
    lk = multi_link()
    cycle(lk)
    req(lk, b"\x04")
    assert cycle(lk) == []
    assert req(lk, b"\x03") == 1                               # wakes the loop
    assert kinds(cycle(lk)) == ["hi", "usage", "se", "ac", "sy"]


def test_hi_carries_the_ascii_computer_name(monkeypatch):
    import socket
    monkeypatch.setenv("COMPUTERNAME", "BÜRO-RECHNER-NUMMER-ZWEI")
    assert cm.pc_name() == "BRO-RECHNER-NUM" and len(cm.pc_name()) <= cm.PC_NAME_MAX
    monkeypatch.setenv("COMPUTERNAME", "ÄÖÜ")                  # nothing left: hostname
    monkeypatch.setattr(socket, "gethostname", lambda: "lab-box")
    assert cm.pc_name() == "lab-box"
    monkeypatch.setattr(socket, "gethostname", lambda: "")
    assert cm.pc_name() == "PC"
    monkeypatch.delenv("COMPUTERNAME")
    assert cm.pc_name() == "PC"
    # Small packets: the name is cut until the message fits, never emptied.
    monkeypatch.setenv("COMPUTERNAME", "WORKSTATION-001")
    msg = multi_link(mtu=23)._hi_message()
    assert len(json.dumps(msg, separators=(",", ":"))) <= 20 and msg["n"]
    assert "WORKSTATION-001".startswith(msg["n"])
    assert multi_link()._hi_message() == {"k": "hi", "n": "WORKSTATION-001"}


def test_hi_needs_protocol_2():
    import asyncio
    client = FakeClient()
    asyncio.run(multi_link(proto=1)._send_hi(client))
    assert client.writes == []


def run_loop():
    import asyncio
    import threading
    loop = asyncio.new_event_loop()
    th = threading.Thread(target=loop.run_forever, daemon=True)
    th.start()
    return loop, th


def stop_loop(loop, th):
    loop.call_soon_threadsafe(loop.stop)
    th.join(2)
    loop.close()


def test_request_show_sends_use_on_the_live_connection():
    lk = multi_link()
    assert lk.request_show() is False                          # not connected
    loop, th = run_loop()
    try:
        client = FakeClient()
        lk._live = (loop, client)
        assert lk.request_show() is True
        assert client.writes == [{"k": "use"}]
        lk._proto = 1                                          # old firmware: no "use"
        assert lk.request_show() is False and client.writes == [{"k": "use"}]
    finally:
        stop_loop(loop, th)
    lk._proto = 2
    assert lk.request_show() is False                          # loop gone


def test_request_show_reports_a_failed_write():
    class Broken:
        async def write_gatt_char(self, *_a, **_k):
            raise OSError("gone")
    logs = []
    lk = cm.ClawdmeterLink(log=logs.append)
    lk._proto = 2
    loop, th = run_loop()
    try:
        lk._live = (loop, Broken())
        assert lk.request_show() is False
        assert any("show request failed" in m for m in logs)
    finally:
        stop_loop(loop, th)


def test_session_says_hi_on_connect_and_forgets_shown_after(monkeypatch):
    """A whole connection with a fake BleakClient: hi right after subscribing
    to REQ, then the normal pass; afterwards shown and the live handle reset."""
    import asyncio
    import pytest
    bleak = pytest.importorskip("bleak")
    monkeypatch.setenv("COMPUTERNAME", "DESK-PC")
    order = []

    class Svc:
        uuid = cm.SERVICE_UUID

    class FakeBleak:
        def __init__(self, *_a, **_k):
            self.services = [Svc()]
            self.mtu_size = 247
            self.is_connected = True

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def read_gatt_char(self, uuid):
            if uuid == cm.CAPS_CHAR_UUID:
                return b"2"
            raise OSError("no battery service")

        async def start_notify(self, uuid, cb):
            order.append(("notify", uuid))

        async def write_gatt_char(self, _uuid, data, response=False):
            order.append(("write", json.loads(bytes(data))))

    monkeypatch.setattr(bleak, "BleakClient", FakeBleak)

    def system():
        lk._stop.set()                 # end the session after the first pass
        return SY

    ex = multi_extras()
    lk = cm.ClawdmeterLink(session_provider=lambda: {"n": "", "h": ""},
                           usage_provider=lambda: USAGE,
                           extras_provider=lambda: ex, system_provider=system)
    lk._set(shown=False)               # left over from an earlier connection
    asyncio.run(lk._session("AA:BB:CC:DD:EE:FF"))
    writes = [x[1] for x in order if x[0] == "write"]
    assert order[0] == ("notify", cm.REQ_CHAR_UUID)
    assert writes[0] == {"k": "hi", "n": "DESK-PC"}
    assert kinds(writes) == ["hi", "usage", "se", "ac", "sy"]
    assert lk.status()["shown"] is None and lk._live is None
