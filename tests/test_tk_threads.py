"""The buddy and the limit-reset card run tkinter on their own threads. Their Tk
root stays in a reference cycle after the window closes; if the garbage collector
frees it on another thread, Tcl aborts the whole app (Tcl_AsyncDelete). These
tests run that scenario in a child process, because the failure kills the process."""
import os
import subprocess
import sys
import textwrap

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SCRIPT = textwrap.dedent("""
    import gc, sys, threading
    sys.path.insert(0, {root!r})
    import claude_sessions as cs

    def body():
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        steps = {{"n": 0}}

        def tick():  # calls itself through its closure, like slide/fade/tick in the app
            steps["n"] += 1
            if steps["n"] >= 3:
                root.destroy()
                return
            root.after(10, tick)

        root.after(10, tick)
        root.mainloop()

    gc.disable()  # nothing collects by itself; only the explicit calls below
    t = threading.Thread(target=cs._run_tk_thread, args=(body,), daemon=True)
    t.start()
    t.join()
    gc.collect()  # another thread collecting: aborts the process if the Tk root is still there
    print("ok")
""")


def test_a_tk_thread_frees_its_window_on_its_own_thread():
    pytest.importorskip("tkinter")
    proc = subprocess.run([sys.executable, "-c", SCRIPT.format(root=ROOT)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().endswith("ok")


def test_buddy_and_reset_card_threads_go_through_the_helper(monkeypatch):
    import claude_sessions as cs
    calls = []
    monkeypatch.setattr(cs, "_run_tk_thread", lambda body, *a: calls.append((body.__name__, a)))
    cs.LimitResetToast()._run("t", "s", None)
    buddy = cs.BuddyController.__new__(cs.BuddyController)
    buddy._run()
    assert calls == [("_run_tk", ("t", "s", None)), ("_run_tk", ())]
