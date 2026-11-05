from datetime import datetime
from pathlib import Path

from launcher.display import age, middle_ellipsis, rel_or_abs


def test_middle_ellipsis_keeps_last_two_segments():
    last = r"Completion_Handler_-JBF-_New_user_2026-08-25"
    out = middle_ellipsis(rf"work\wf\{last}", 40)
    assert out.endswith(rf"\{last}"[-30:])
    assert out.endswith(rf"wf\{last}")  # last two segments are never truncated


def test_middle_ellipsis_drops_middle_when_needed():
    p = r"work\clients\bast\bifrost\deploy\scripts"
    assert middle_ellipsis(p, 100) == p
    assert middle_ellipsis(p, 30) == "work\\…\\deploy\\scripts"


def test_middle_ellipsis_truncates_first_segment_before_last_two():
    out = middle_ellipsis(r"averyveryverylongfirstsegment\a\b\cc\dd", 14)
    assert out == "avery…\\…\\cc\\dd"


def test_middle_ellipsis_short_paths():
    assert middle_ellipsis("work", 10) == "work"
    assert middle_ellipsis(r"work\net", 20) == r"work\net"
    assert middle_ellipsis(r"verylongfirst\net", 10) == "veryl…\\net"


def test_rel_or_abs(fake_env):
    r = fake_env.root
    assert rel_or_abs(r, r / "work" / "net") == str(Path("work") / "net")
    other = r.parent / "elsewhere"
    assert rel_or_abs(r, other) == str(other)
    assert rel_or_abs(r, r) == "."


def test_age():
    now = 1_700_000_000.0
    assert age(now - 2 * 3600, now) == "2h"
    assert age(now - 3 * 86400, now) == "3d"
    assert age(now - 5 * 60, now) == "5m"
    assert age(now - 10, now) == "now"
    assert age(None, now) == "—"
    old = now - 60 * 86400
    assert age(old, now) == datetime.fromtimestamp(old).strftime("%b")
