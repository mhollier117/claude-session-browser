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


def test_switch_request_without_callback_is_a_refresh():
    lk = cm.ClawdmeterLink()
    woke = []
    lk._handle_request(b"\x02\x00a", lambda: woke.append(1))
    assert woke == [1]
