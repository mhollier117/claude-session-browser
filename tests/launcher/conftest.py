# No __init__.py in tests/launcher: a `tests/launcher` package would shadow the real `launcher` package,
# so test file basenames under tests/launcher must stay unique across tests/.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, so `launcher` imports regardless of cwd

import pytest

from launcher.paths import Env

_TREE = ("work/clients/bast/bifrost", "work/net", "work/halo", "work/vendors", "home/homelab", "home/ai", "cc")


@pytest.fixture(autouse=True)
def _fresh_folder_walk():
    """The suggester caches its folder walk per process for 60 s; a test that adds folders between two
    suggestions must not see the stale walk."""
    from launcher.retrieve import invalidate_walk
    invalidate_walk()
    yield
    invalidate_walk()


@pytest.fixture
def fake_env(tmp_path) -> Env:
    root = tmp_path / "root"
    for rel in _TREE:
        (root / rel).mkdir(parents=True)
    env = Env(root=root, claude_home=tmp_path / "claude", qwen_home=tmp_path / "qwen",
              data_dir=tmp_path / "data", now=lambda: 1_700_000_000.0)
    for home in (env.claude_home, env.qwen_home):
        (home / "projects").mkdir(parents=True)
        (home / "sessions").mkdir()
    return env
