"""What the user did in the new-session walk, for tuning: one JSON line per event in `data_dir/flow.log`.

`log(env, event, **fields)` writes `{"ts", "event", **fields}`; the new-session flow passes the folder (relative to
the root), the row kind, the filter text and the step. Past `MAX_BYTES` the file keeps only its newer half (cut at a
line boundary). Logging never raises: the log is diagnostic only."""
from __future__ import annotations

import json
import os
import threading

from .paths import Env

MAX_BYTES = 1_000_000
FILE = "flow.log"

_lock = threading.Lock()


def _tmp_path(path):
    """The temp file `_trim` writes: per process, so two `cc` processes trimming at once never share one."""
    return path.with_name(f"{path.name}.{os.getpid()}.tmp")


def _trim(path) -> None:
    data = path.read_bytes()
    if len(data) <= MAX_BYTES:
        return
    tail = data[len(data) // 2:]
    cut = tail.find(b"\n")
    tail = tail[cut + 1:] if cut >= 0 else b""
    tmp = _tmp_path(path)
    try:
        tmp.write_bytes(tail)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def log(env: Env, event: str, **fields) -> None:
    """Append one event line; trims the file to its newer half once it passes `MAX_BYTES`."""
    entry = {"ts": env.now(), "event": event, **fields}
    try:
        line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
        env.data_dir.mkdir(parents=True, exist_ok=True)
        path = env.data_dir / FILE
        with _lock:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
            if path.stat().st_size > MAX_BYTES:
                _trim(path)
    except (OSError, TypeError, ValueError):
        pass
