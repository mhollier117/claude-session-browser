"""A second start must never run alongside the first: it hands over to the running
window, or waits until the first one is gone (an update), never both running."""
import claude_sessions as cs


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


def test_every_handle_that_does_not_own_the_guard_is_closed():
    f = Fake(free_at=2.0)
    f.wait()
    assert f.closed == ["h0", "h1", "h2", "h3"]
