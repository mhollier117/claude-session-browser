"""Installing/removing the naming gate and the Notification hook in a fake
~/.claude/settings.json. Other hooks and settings must survive untouched."""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
pytest.importorskip("webview")
import claude_sessions as cs  # noqa: E402

FOREIGN = {"type": "command", "command": "powershell.exe -File other.ps1"}


@pytest.fixture
def fake(tmp_path, monkeypatch):
    cfg = tmp_path / ".claude"
    cfg.mkdir()
    settings = cfg / "settings.json"
    dest = cfg / "hooks" / cs.NAME_GATE_FILE
    monkeypatch.setattr(cs, "CLAUDE_SETTINGS_FILE", str(settings))
    monkeypatch.setattr(cs, "NAME_GATE_DEST", str(dest))
    return settings, dest


def _write(path, d):
    path.write_text(json.dumps(d), encoding="utf-8")


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _gate_cmds(d):
    return [h["command"] for g in d.get("hooks", {}).get("UserPromptSubmit", [])
            for h in g.get("hooks", []) if cs.NAME_GATE_FILE in h["command"]]


def test_on_off_roundtrip_keeps_everything_else(fake):
    settings, dest = fake
    original = {"model": "opus", "enabledPlugins": {"x@y": True},
                "hooks": {"UserPromptSubmit": [{"hooks": [FOREIGN]}],
                          "Notification": [{"matcher": "", "hooks": [FOREIGN]}]}}
    _write(settings, original)

    assert cs.name_gate_einrichten(True)
    d = _read(settings)
    assert len(_gate_cmds(d)) == 1
    assert d["hooks"]["UserPromptSubmit"][0] == {"hooks": [FOREIGN]}
    assert d["model"] == "opus" and d["enabledPlugins"] == {"x@y": True}
    assert dest.read_bytes() == Path(cs._name_gate_quelle()).read_bytes()
    assert cs.name_gate_eingerichtet()
    gate = d["hooks"]["UserPromptSubmit"][-1]["hooks"][0]
    assert gate["timeout"] == 10 and str(dest) in gate["command"]

    assert cs.name_gate_einrichten(True)          # idempotent: still one entry
    assert len(_gate_cmds(_read(settings))) == 1

    assert cs.name_gate_einrichten(False)
    assert _read(settings) == original
    assert not dest.exists()
    assert not cs.name_gate_eingerichtet()


def test_on_without_settings_file(fake):
    settings, dest = fake
    assert cs.name_gate_einrichten(True)
    assert len(_gate_cmds(_read(settings))) == 1
    assert cs.name_gate_einrichten(False)
    assert _read(settings) == {}


def test_unreadable_settings_are_not_overwritten(fake):
    settings, dest = fake
    settings.write_text("{ this is not json", encoding="utf-8")
    assert cs.name_gate_einrichten(True) is False
    assert settings.read_text(encoding="utf-8") == "{ this is not json"
    assert cs.hooks_einrichten(True) is False
    assert settings.read_text(encoding="utf-8") == "{ this is not json"


def test_missing_script_source_refuses(fake, monkeypatch):
    settings, dest = fake
    _write(settings, {"model": "opus"})
    monkeypatch.setattr(cs, "_name_gate_quelle", lambda: str(dest) + ".nope")
    assert cs.name_gate_einrichten(True) is False
    assert _read(settings) == {"model": "opus"}


def test_entry_without_script_file_is_not_installed(fake):
    settings, dest = fake
    assert cs.name_gate_einrichten(True)
    os.remove(dest)
    assert not cs.name_gate_eingerichtet()


def test_notification_hook_unchanged_behaviour(fake):
    settings, dest = fake
    _write(settings, {"hooks": {"Notification": [{"matcher": "p",
                                                  "hooks": [FOREIGN]}]}})
    assert cs.hooks_einrichten(True)
    assert cs.hooks_eingerichtet()
    d = _read(settings)
    assert d["hooks"]["Notification"][0] == {"matcher": "p", "hooks": [FOREIGN]}
    assert cs._HOOK_MARKE in d["hooks"]["Notification"][1]["hooks"][0]["command"]
    assert cs.hooks_einrichten(False)
    assert _read(settings) == {"hooks": {"Notification": [
        {"matcher": "p", "hooks": [FOREIGN]}]}}


def test_gate_and_notification_hooks_are_independent(fake):
    settings, dest = fake
    assert cs.hooks_einrichten(True) and cs.name_gate_einrichten(True)
    assert cs.name_gate_einrichten(False)
    assert cs.hooks_eingerichtet() and not cs.name_gate_eingerichtet()


def test_api_bridge(fake):
    a = cs.Api.__new__(cs.Api)
    assert a.name_gate_state()["on"] is False
    assert a.name_gate_toggle(True) == {"ok": True, "on": True}
    assert a.name_gate_toggle(False) == {"ok": True, "on": False}


def test_installer_default_respects_a_deliberate_off(fake, tmp_path, monkeypatch):
    settings, dest = fake
    app_settings = tmp_path / "csb_settings.json"
    monkeypatch.setattr(cs, "SETTINGS_FILE", str(app_settings))
    a = cs.Api.__new__(cs.Api)
    a.settings = {}

    assert cs.name_gate_standard() and cs.name_gate_eingerichtet()   # fresh install: on
    assert a.name_gate_toggle(False)["on"] is False                  # user turns it off
    assert _read(app_settings)["name_gate_declined"] is True
    assert cs.name_gate_standard() and not cs.name_gate_eingerichtet()  # update keeps it off
    assert a.name_gate_toggle(True)["on"] is True                    # back on in the app
    assert _read(app_settings)["name_gate_declined"] is False
    os.remove(dest)                                                  # update refreshes script
    assert cs.name_gate_standard() and dest.exists()
