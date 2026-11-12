"""A second start must never run alongside the first: it hands over to the running
window, or waits until the first one is gone (an update), never both running."""
import os
import subprocess
import sys
import textwrap
import uuid

import pytest

import claude_sessions as cs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

GUARD = textwrap.dedent("""
    import sys, time
    sys.path.insert(0, {root!r})
    import claude_sessions as cs
    cs._SINGLE_INSTANCE_MUTEX = {mutex!r}
    cs._SINGLE_INSTANCE_LOCKNAME = {lock!r}
    owned, handle = cs._acquire_single_instance()
    print("owned" if owned else "second", flush=True)
    if {hold}:
        time.sleep(30)
""")


def _guard(tmp_path, mutex, lock, hold):
    env = dict(os.environ, LOCALAPPDATA=str(tmp_path))
    code = GUARD.format(root=ROOT, mutex=mutex, lock=lock, hold=hold)
    return subprocess.Popen([sys.executable, "-c", code], env=env, stdout=subprocess.PIPE, text=True)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows mutex and lock file")
@pytest.mark.parametrize("which", ["both", "mutex only", "lock file only"])
def test_a_second_process_is_always_told_it_is_second(tmp_path, which):
    """Each guard on its own must recognise the running copy: the mutex check used to
    lose the 'already exists' code, the lock file check counted its own failure as 'first'."""
    tag = uuid.uuid4().hex
    first = _guard(tmp_path, "Local\\csb-test-" + tag, "lock-" + tag, hold=True)
    try:
        assert first.stdout.readline().strip() == "owned"
        mutex = "Local\\csb-test-" + (tag if which != "lock file only" else uuid.uuid4().hex)
        lock = "lock-" + (tag if which != "mutex only" else uuid.uuid4().hex)
        for _ in range(5):
            second = _guard(tmp_path, mutex, lock, hold=False)
            out, _err = second.communicate(timeout=60)
            assert out.strip() == "second"
    finally:
        first.kill()
        first.wait()


class Fake:
    def __init__(self, window_at=None, free_at=None):
        self.t = 0.0
        self.window_at, self.free_at = window_at, free_at
        self.closed = []
        self.slept = 0

    def clock(self):
        return self.t

    def sleep(self, s):
        self.t += s
        self.slept += 1

    def restore(self):
        return self.window_at is not None and self.t >= self.window_at

    def acquire(self):
        if self.free_at is not None and self.t >= self.free_at:
            return True, "mine"
        return False, f"h{self.slept}"

    def close(self, h):
        self.closed.append(h)

    def wait(self, timeout=20.0):
        return cs._wait_for_instance_slot(timeout=timeout, step=0.5, acquire=self.acquire,
                                          restore=self.restore, close=self.close,
                                          sleep=self.sleep, clock=self.clock)


def test_hands_over_to_a_running_window_at_once():
    f = Fake(window_at=0.0)
    assert f.wait() == (False, None)
    assert f.slept == 0


def test_waits_for_a_first_instance_still_starting_then_hands_over():
    f = Fake(window_at=4.0)
    assert f.wait() == (False, None)
    assert f.t == 4.0


def test_becomes_the_instance_when_the_old_one_exits():
    f = Fake(free_at=3.0)
    assert f.wait() == (True, "mine")


def test_never_starts_a_second_copy_when_the_first_shows_no_window():
    f = Fake()
    assert f.wait(timeout=5.0) == (False, None)
    assert f.t >= 5.0


def test_each_start_is_logged_with_its_parent(tmp_path, monkeypatch):
    import json
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    cs._log_startup("running", first_try=True)
    cs._log_startup("handed over")
    rows = [json.loads(line) for line in open(cs._startup_log_path(), encoding="utf-8")]
    assert [r["outcome"] for r in rows] == ["running", "handed over"]
    assert rows[0]["first_try"] is True and rows[0]["pid"] == os.getpid()
    assert rows[0]["parent"] and rows[0]["parent_name"]


def test_every_handle_that_does_not_own_the_guard_is_closed():
    f = Fake(free_at=2.0)
    f.wait()
    assert f.closed == ["h0", "h1", "h2", "h3"]
