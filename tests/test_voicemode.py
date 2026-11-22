"""voicemode: which push-to-talk the Clawdmeter's Voice key holds."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import voicemode as vm  # noqa: E402
from voicemode import CLAUDE, HANDY, OTHER  # noqa: E402


def test_third_party_cmdline_recognises_the_aliases():
    exe = r"C:\Users\x\.local\bin\claude.exe"
    base = r"C:\Users\x\.claude\claudez"
    assert vm.third_party_cmdline([exe, "--settings", base + r"\launch-settings.json"])              # claudez
    assert vm.third_party_cmdline([exe, "--settings", base + r"\launch-settings-claudek.json"])      # claudek
    assert vm.third_party_cmdline([exe, "--mcp-config", "q.json", "--settings",
                                   base + r"\launch-settings-claudeq.json"])                       # claudeq
    assert vm.third_party_cmdline([exe, "--settings=" + base + r"\launch-settings.json"])
    assert vm.third_party_cmdline([exe, "--settings", '"' + base + r'\Launch-Settings.json"'])


def test_plain_claude_is_not_third_party():
    exe = r"C:\Users\x\.local\bin\claude.exe"
    assert not vm.third_party_cmdline([exe])
    assert not vm.third_party_cmdline([exe, "--dangerously-skip-permissions"])
    assert not vm.third_party_cmdline([exe, "--settings", r"C:\proj\.claude\settings.json"])
    assert not vm.third_party_cmdline([exe, "--settings"])                  # no value
    assert not vm.third_party_cmdline([exe, "launch-settings.json"])        # not after --settings
    assert not vm.third_party_cmdline(None)


def test_cache_reads_each_process_once_and_retries_failures():
    calls = []

    def cmdline_of(pid):
        calls.append(pid)
        if pid == 3:
            raise OSError("gone")
        return ["claude.exe", "--settings", "launch-settings.json"] if pid == 1 else ["claude.exe"]

    tp = vm.ThirdPartyCache(cmdline_of)
    a = {"pid": 1, "started": 10.0}
    b = {"pid": 2, "started": 11.0}
    c = {"pid": 3, "started": 12.0}
    assert tp(a) and tp(a)
    assert not tp(b) and not tp(b)
    assert not tp(c) and not tp(c)
    assert calls == [1, 2, 3, 3]                       # failures are not cached
    assert not tp({"pid": None})


def _sessions():
    return [{"name": "Clawdmeter", "pid": 1}, {"name": "rev1 h2d", "pid": 2},
            {"name": "same", "pid": 3}, {"name": "same", "pid": 4}]


def _tp(third):
    return lambda s: s["pid"] in third


def test_pick_follows_the_window_in_front():
    s = _sessions()
    assert vm.pick(s, "\u25d0 Clawdmeter", _tp({2})) == CLAUDE      # spinner glyph stripped
    assert vm.pick(s, "\u2733 rev1 h2d", _tp({2})) == HANDY         # claudek session
    assert vm.pick(s, "clawdmeter", _tp(set())) == CLAUDE           # case-insensitive
    assert vm.pick(s, "Mozilla Firefox", _tp(set())) == OTHER       # not a session
    assert vm.pick(s, "", _tp(set())) == OTHER                      # no window
    assert vm.pick([], "Clawdmeter", _tp(set())) == OTHER


def test_pick_with_a_shared_name_needs_all_to_be_anthropic():
    s = _sessions()
    assert vm.pick(s, "same", _tp(set())) == CLAUDE
    assert vm.pick(s, "same", _tp({4})) == HANDY


def test_link_sends_vk_only_for_known_modes():
    from test_clawdmeter_payload import link
    assert link({"se": [], "ac": [], "vk": "h"})._extras_messages()["vk"] == '{"k":"vk","m":"h"}'
    assert link({"se": [], "ac": [], "vk": "c"})._extras_messages()["vk"] == '{"k":"vk","m":"c"}'
    assert link({"se": [], "ac": [], "vk": "o"})._extras_messages()["vk"] == '{"k":"vk","m":"o"}'
    for bad in (None, "x", ""):
        assert "vk" not in link({"se": [], "ac": [], "vk": bad})._extras_messages()
