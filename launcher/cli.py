"""`cc`: the terminal entry point.

    cc              refresh the index, show the resume list
    cc new [name]   start a new session (Task 9)
    cc index        refresh the index only; prints a one-line count on stderr
    cc --json       refresh, then print every row as a JSON array (for the browser and scripts)

stdout carries exactly one thing: the single JSON line of `launch.to_shell_result` when a launch was chosen
(the PowerShell `cc` function in tools/cc.ps1 reads it, `cd`s and runs the wrapper in the same tab), or the
`--json` array. Everything the user sees goes to the terminal's alternate screen or to stderr.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import asdict

from .index import Index
from .launch import Launch, to_shell_result
from .paths import Env, default_env
from .tui.resume import NEW, Resume

AppFn = Callable[[Env, Index], object]


def _utf8_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError, OSError):
        pass  # replaced or detached stream (tests, embedding): leave it as it is


def _emit(text: str) -> None:
    _utf8_stdout()
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _progress() -> Callable[[int, int], None] | None:
    """A one-line `indexing… N/M` on stderr, only when stderr is a terminal."""
    err = sys.stderr
    if err is None or not err.isatty():
        return None

    def report(done: int, total: int) -> None:
        if done == total or done % 50 == 0:
            err.write(f"\rindexing… {done}/{total}")
            err.flush()
        if done == total:
            err.write("\r" + " " * (len(f"indexing… {done}/{total}") + 1) + "\r")
            err.flush()
    return report


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _run_new(env: Env, index: Index, name: str | None) -> int:
    """Task 9 hook: the new-session walk. Until then `cc new` and `n` exit quietly with nothing on stdout."""
    return 0


def _finish(result: object, env: Env, index: Index) -> int:
    if isinstance(result, Resume):
        if result.row.cwd_exists:  # a missing folder: cc.ps1 shows the warning and refuses, so nothing ran
            index.record_launch(result.row.store, result.row.sid, result.wrapper)
        _emit(to_shell_result(result.launch))
    elif isinstance(result, Launch):
        _emit(to_shell_result(result))
    elif result is NEW:
        return _run_new(env, index, None)
    return 0  # quit, or a live session's window was focused: nothing to run


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cc", description="Resume or start Claude Code / Qwen sessions.")
    p.add_argument("--json", action="store_true", help="print every indexed session as a JSON array")
    sub = p.add_subparsers(dest="command")
    sub.add_parser("index", help="refresh the session index and print a count on stderr")
    new = sub.add_parser("new", help="start a new session")
    new.add_argument("name", nargs="?", help="session name")
    return p


def main(argv: list[str] | None = None, *, env: Env | None = None, index: Index | None = None,
         app: AppFn | None = None) -> int:
    """`env`, `index` and `app` are injectable for tests; `app(env, index)` runs the resume screen and returns
    a `Resume`, a `Launch`, `NEW`, `FOCUSED` or None."""
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    env = env or default_env()
    own_index = index is None
    index = index or Index(env)
    try:
        if args.command == "new":
            return _run_new(env, index, args.name)
        return _main(args, env, index, app)
    except KeyboardInterrupt:  # ctrl-c during the refresh (the app itself maps ctrl-c to a quiet quit)
        sys.stderr.write("\n")
        return 130
    finally:
        if own_index:
            index.close()


def _main(args: argparse.Namespace, env: Env, index: Index, app: AppFn | None) -> int:
    touched = index.refresh(progress=_progress())
    if args.command == "index":
        sys.stderr.write(f"{_plural(len(index.rows()), 'session')} indexed, {touched} changed\n")
        return 0
    if args.json:
        _emit(json.dumps([asdict(r) for r in index.rows()], ensure_ascii=False))
        return 0
    if app is None:
        from .tui.app import NoTerminal, run_resume
        try:
            result = run_resume(env, index)
        except NoTerminal as exc:
            sys.stderr.write(f"cc: {exc}\n")
            return 1
    else:
        result = app(env, index)
    return _finish(result, env, index)

if __name__ == "__main__":
    sys.exit(main())
