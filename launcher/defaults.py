"""Remembered launch choices per bucket, so the new-session flow can preselect them.

A bucket is the first two relative parts under the tree root when the first is `work` (`work\\wf`, `work\\clients`,
`work\\halo`, ...), else the first part (`home`, `cc`); a folder outside the root is `other`. The choices live in
`data_dir/defaults.json` as `{bucket: {"wrapper": ..., "github": ...}}`. Nothing remembered (or a broken file)
falls back to the built-ins: the wrapper from `launch.default_wrapper`, GitHub `none` for `work\\wf` (a Rewst
workflow-export folder gets no repo) and `personal` elsewhere.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .launch import WRAPPERS, default_wrapper
from .paths import Env

FILE = "defaults.json"
GITHUB_CHOICES = ("personal", "work", "none")  # the order `g` cycles through
_NO_REPO_BUCKETS = ("work\\wf",)
_OUTSIDE = "other"
_RETRY_S = 0.05


@dataclass(frozen=True)
class Defaults:
    wrapper: str
    github: str  # personal | work | none


def bucket_key(root: Path, folder: Path) -> str:
    """The key `folder` is remembered under (lower case, `\\`-joined)."""
    base = [p.lower() for p in Path(root).parts]
    parts = [p.lower() for p in Path(folder).parts]
    if parts[:len(base)] != base:
        return _OUTSIDE
    rel = parts[len(base):]
    if not rel:
        return _OUTSIDE
    return "\\".join(rel[:2] if rel[0] == "work" else rel[:1])


def _load(env: Env) -> dict:
    """The stored buckets; any failure at all means none (for `get`, which then uses the built-ins)."""
    try:
        return _read(env)
    except OSError:
        return {}


def _read(env: Env) -> dict:
    """The stored buckets. A missing file, a broken one or a top level that is not an object is `{}`; any other
    OSError (a sharing violation while another writer replaces the file) is retried once, then raised."""
    path = env.data_dir / FILE
    for attempt in (1, 2):
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except UnicodeDecodeError:
            return {}
        except OSError:
            if attempt == 2:
                raise
            time.sleep(_RETRY_S)
            continue
        try:
            data = json.loads(text)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}
    return {}


def get(env: Env, folder: Path) -> Defaults:
    """The wrapper and GitHub choice to preselect for `folder`; each falls back separately to its built-in."""
    key = bucket_key(env.root, folder)
    saved = _load(env).get(key)
    saved = saved if isinstance(saved, dict) else {}
    wrapper, github = saved.get("wrapper"), saved.get("github")
    if wrapper not in WRAPPERS:
        wrapper = default_wrapper(env.root, folder)
    if github not in GITHUB_CHOICES:
        github = "none" if key in _NO_REPO_BUCKETS else "personal"
    return Defaults(wrapper, github)


def remember(env: Env, folder: Path, wrapper: str, github: str | None = None) -> None:
    """Remember `wrapper` (and `github`, when given; None keeps the earlier one) for `folder`'s bucket. Raises
    ValueError for an unknown value and OSError when the file cannot be written."""
    if wrapper not in WRAPPERS:
        raise ValueError(f"unknown wrapper: {wrapper!r}")
    if github is not None and github not in GITHUB_CHOICES:
        raise ValueError(f"unknown GitHub choice: {github!r}")
    key = bucket_key(env.root, folder)
    env.data_dir.mkdir(parents=True, exist_ok=True)
    data = _read(env)  # raises when the file cannot be read: writing now would erase the other buckets
    entry = data.get(key) if isinstance(data.get(key), dict) else {}
    entry = {**entry, "wrapper": wrapper}
    if github is not None:
        entry["github"] = github
    data[key] = entry
    path = env.data_dir / FILE
    tmp = path.with_name(f"{FILE}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
