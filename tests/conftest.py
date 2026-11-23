"""Suite-wide guards: no test reads the real `~/.codex`, and codex_store's module caches start empty per test."""
import pytest


@pytest.fixture(autouse=True)
def _hermetic_codex(tmp_path_factory, monkeypatch):
    """`CODEX_HOME` points at an empty folder unless a test sets its own (codex_store.codex_home and
    launcher.paths read it); codex_store's per-home caches are reset before and after each test."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path_factory.mktemp("no-codex-home") / "codex"))
    import codex_store
    codex_store._reset_caches()
    yield
    codex_store._reset_caches()
