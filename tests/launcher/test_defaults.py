import json

import pytest

from launcher import defaults
from launcher.defaults import Defaults, bucket_key


@pytest.mark.parametrize("rel, key", [
    ("work/wf/mediko_user_creation", "work\\wf"),
    ("work/wf", "work\\wf"),
    ("work/clients/bast/bifrost", "work\\clients"),
    ("work/halo/view_management", "work\\halo"),
    ("work", "work"),
    ("home/homelab/pxe", "home"),
    ("cc/claude_improvements", "cc"),
    ("", "other"),
])
def test_bucket_key(fake_env, rel, key):
    assert bucket_key(fake_env.root, fake_env.root / rel) == key


def test_bucket_key_outside_the_root_is_other(fake_env, tmp_path):
    assert bucket_key(fake_env.root, tmp_path / "elsewhere" / "x") == "other"


def test_bucket_key_ignores_case(fake_env):
    assert bucket_key(fake_env.root, fake_env.root / "Work" / "WF" / "X") == "work\\wf"


@pytest.mark.parametrize("rel, expected", [
    ("work/wf/export", Defaults("clauded", "none")),
    ("work/net", Defaults("clauded", "personal")),
    ("work/clients/bast", Defaults("clauded", "personal")),
    ("home/ai", Defaults("clauded", "personal")),
    ("cc/tools", Defaults("claude", "personal")),
])
def test_built_in_fallbacks(fake_env, rel, expected):
    assert defaults.get(fake_env, fake_env.root / rel) == expected


def test_remember_get_round_trip_per_bucket(fake_env):
    r = fake_env.root
    defaults.remember(fake_env, r / "work" / "wf" / "a", "claudeb", "personal")
    defaults.remember(fake_env, r / "work" / "clients" / "bast", "claude6", "work")
    assert defaults.get(fake_env, r / "work" / "wf" / "other") == Defaults("claudeb", "personal")
    assert defaults.get(fake_env, r / "work" / "clients" / "x") == Defaults("claude6", "work")
    assert defaults.get(fake_env, r / "work" / "net") == Defaults("clauded", "personal")  # untouched bucket
    saved = json.loads((fake_env.data_dir / "defaults.json").read_text(encoding="utf-8"))
    assert saved == {"work\\wf": {"wrapper": "claudeb", "github": "personal"},
                     "work\\clients": {"wrapper": "claude6", "github": "work"}}


def test_remember_without_github_keeps_the_earlier_one(fake_env):
    folder = fake_env.root / "home" / "ai"
    defaults.remember(fake_env, folder, "clauded", "none")
    defaults.remember(fake_env, folder, "qwen")
    assert defaults.get(fake_env, folder) == Defaults("qwen", "none")


def test_remember_rejects_unknown_values(fake_env):
    folder = fake_env.root / "home"
    with pytest.raises(ValueError):
        defaults.remember(fake_env, folder, "bash")
    with pytest.raises(ValueError):
        defaults.remember(fake_env, folder, "clauded", "gitlab")
    assert not (fake_env.data_dir / "defaults.json").exists()


def test_broken_or_invalid_file_falls_back(fake_env):
    fake_env.data_dir.mkdir(parents=True)
    path = fake_env.data_dir / "defaults.json"
    path.write_text("{not json", encoding="utf-8")
    assert defaults.get(fake_env, fake_env.root / "home") == Defaults("clauded", "personal")
    path.write_text(json.dumps({"home": {"wrapper": "bash", "github": "gitlab"}, "cc": "x"}), encoding="utf-8")
    assert defaults.get(fake_env, fake_env.root / "home") == Defaults("clauded", "personal")
    assert defaults.get(fake_env, fake_env.root / "cc") == Defaults("claude", "personal")
    defaults.remember(fake_env, fake_env.root / "home", "claudew", "work")  # replaces the broken content
    assert defaults.get(fake_env, fake_env.root / "home") == Defaults("claudew", "work")


# ---- review fixes: a failed read never erases the other buckets ------------------------------------------------------

def _seed(fake_env):
    defaults.remember(fake_env, fake_env.root / "home", "claudeb", "work")
    defaults.remember(fake_env, fake_env.root / "cc", "claude6", "none")
    return (fake_env.data_dir / "defaults.json").read_bytes()


def _failing_reads(monkeypatch, failures):
    from pathlib import Path
    real = Path.read_text
    state = {"left": failures}

    def read_text(self, *a, **k):
        if self.name == "defaults.json" and state["left"] > 0:
            state["left"] -= 1
            raise PermissionError("sharing violation")
        return real(self, *a, **k)
    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(defaults, "_RETRY_S", 0)


def test_remember_raises_and_leaves_the_file_alone_when_the_read_keeps_failing(fake_env, monkeypatch):
    before = _seed(fake_env)
    _failing_reads(monkeypatch, 2)
    with pytest.raises(PermissionError):
        defaults.remember(fake_env, fake_env.root / "work" / "net", "claudew", "none")
    assert (fake_env.data_dir / "defaults.json").read_bytes() == before
    assert [p.name for p in fake_env.data_dir.iterdir()] == ["defaults.json"]


def test_remember_retries_a_failed_read_once_and_keeps_the_other_buckets(fake_env, monkeypatch):
    _seed(fake_env)
    _failing_reads(monkeypatch, 1)
    defaults.remember(fake_env, fake_env.root / "work" / "net", "claudew", "none")
    assert defaults.get(fake_env, fake_env.root / "home") == Defaults("claudeb", "work")
    assert defaults.get(fake_env, fake_env.root / "cc") == Defaults("claude6", "none")
    assert defaults.get(fake_env, fake_env.root / "work" / "net") == Defaults("claudew", "none")


def test_get_stays_tolerant_when_the_read_fails(fake_env, monkeypatch):
    _seed(fake_env)
    _failing_reads(monkeypatch, 5)
    assert defaults.get(fake_env, fake_env.root / "home") == Defaults("clauded", "personal")


def test_a_failed_replace_leaves_no_temp_file(fake_env, monkeypatch):
    before = _seed(fake_env)

    def boom(src, dst):
        raise PermissionError("locked")
    monkeypatch.setattr(defaults.os, "replace", boom)
    with pytest.raises(PermissionError):
        defaults.remember(fake_env, fake_env.root / "home", "claudew", "none")
    assert [p.name for p in fake_env.data_dir.iterdir()] == ["defaults.json"]
    assert (fake_env.data_dir / "defaults.json").read_bytes() == before


def test_temp_names_are_unique_per_writer(fake_env, monkeypatch):
    names = []
    real = defaults.os.replace
    monkeypatch.setattr(defaults.os, "replace", lambda s, d: names.append(s.name) or real(s, d))
    defaults.remember(fake_env, fake_env.root / "home", "claude")
    defaults.remember(fake_env, fake_env.root / "home", "clauded")
    assert len(set(names)) == 2 and all(n.endswith(".tmp") for n in names)


@pytest.mark.parametrize("bad", [{"a": 1}, ["x"], 7, None])
def test_a_non_string_stored_value_falls_back_without_raising(fake_env, bad):
    fake_env.data_dir.mkdir(parents=True)
    (fake_env.data_dir / "defaults.json").write_text(
        json.dumps({"home": {"wrapper": bad, "github": bad}}), encoding="utf-8")
    assert defaults.get(fake_env, fake_env.root / "home") == Defaults("clauded", "personal")


def test_remember_treats_a_non_object_top_level_as_empty(fake_env):
    fake_env.data_dir.mkdir(parents=True)
    (fake_env.data_dir / "defaults.json").write_text("[1, 2]", encoding="utf-8")
    defaults.remember(fake_env, fake_env.root / "home", "claudeb")
    assert defaults.get(fake_env, fake_env.root / "home").wrapper == "claudeb"
