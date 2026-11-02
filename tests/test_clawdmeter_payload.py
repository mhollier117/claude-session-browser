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
