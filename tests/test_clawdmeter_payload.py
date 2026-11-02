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
    ex = {"ss": [{"n": long, "a": "work coding"} for _ in range(6)],
          "ac": [{"n": "an-extremely-long-nickname", "l": 1, "r": 1, "s": 9, "sr": 180,
                  "w": 32, "wr": 4320, "f": 4, "fr": 5000}]}
    msgs = link(ex, mtu=120)._extras_messages()
    for text in msgs.values():
        assert len(text.encode()) <= 117
    ss = json.loads(msgs["ss"])
    assert ss["k"] == "ss" and 1 <= len(ss["l"]) <= 4 and len(ss["l"][0]["n"]) <= 20
    ac = json.loads(msgs["ac0"])
    assert ac["k"] == "ac" and ac["i"] == 0 and ac["c"] == 1 and len(ac["n"]) <= 14


def test_extras_are_ascii_only():
    msgs = link({"ss": [{"n": "Café ✳ naïve", "a": "done"}], "ac": []})._extras_messages()
    assert json.loads(msgs["ss"])["l"][0]["n"] == "Caf nave"


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
    ex = {"ss": [{"n": "Fix login", "a": "allow", "s": "n", "c": 33, "w": "1M"},
                 {"n": "Fresh", "a": "work coding", "s": "w"}], "ac": []}
    ss = json.loads(link(ex, mtu=247)._extras_messages()["ss"])["l"]
    assert ss == ex["ss"]


def test_long_session_list_shortens_names_before_dropping_sessions():
    names = ["Refactor the payment service", "Fix flaky login test suite",
             "Write release notes draft", "Investigate memory leak now"]
    ex = {"ss": [{"n": n, "a": "work coding", "s": "w", "c": 100, "w": "200K"} for n in names],
          "ac": []}

    def ss_for(mtu):
        text = link(ex, mtu=mtu)._extras_messages()["ss"]
        assert len(text.encode()) <= mtu - 3
        return json.loads(text)["l"]

    roomy = ss_for(400)
    assert [x["n"] for x in roomy] == [cm._ascii(n, cm.SS_NAME_MAX).rstrip() for n in names]
    # Tight: all four still fit once names are cut, none shorter than 12.
    full = len(json.dumps({"k": "ss", "l": roomy}, separators=(",", ":")))
    tight = ss_for(full - 20 + 3)
    assert len(tight) == 4
    assert all(cm.SS_NAME_MIN <= len(x["n"]) < cm.SS_NAME_MAX for x in tight)
    assert all(x["s"] == "w" and x["c"] == 100 and x["w"] == "200K" for x in tight)
    # Tighter than four 12-char names allow: only then sessions go, from the end,
    # and the ones that stay get their longer names back where room allows.
    small = ss_for(244 - 60)
    assert 1 <= len(small) < 4
    assert all(n.startswith(x["n"]) for n, x in zip(names, small))
    assert all(len(x["n"]) >= cm.SS_NAME_MIN for x in small)
    assert len(small[0]["n"]) > cm.SS_NAME_MIN       # room left after dropping: longer again
