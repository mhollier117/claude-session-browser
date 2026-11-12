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
