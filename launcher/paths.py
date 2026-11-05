"""Locations the launcher works with, resolved once and passed around as an `Env`."""
from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Env:
    root: Path  # the project tree the launcher browses
    claude_home: Path
    qwen_home: Path
    data_dir: Path
    now: Callable[[], float] = field(default=time.time)


def default_env() -> Env:
    """Build the real environment; each location can be overridden by an environment variable."""
    home = Path.home()
    local = os.environ.get("LOCALAPPDATA")
    data_default = Path(local) / "cc-launcher" if local else home / ".cc-launcher"
    return Env(
        root=Path(os.environ.get("CC_LAUNCHER_ROOT") or r"C:\claude-code"),
        claude_home=Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude"),
        qwen_home=Path(os.environ.get("CC_LAUNCHER_QWEN_HOME") or home / ".qwen"),
        data_dir=Path(os.environ.get("CC_LAUNCHER_DATA") or data_default),
    )
