from launcher.paths import default_env


def test_default_env_honours_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("CC_LAUNCHER_ROOT", str(tmp_path / "r"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "c"))
    monkeypatch.setenv("CC_LAUNCHER_QWEN_HOME", str(tmp_path / "q"))
    monkeypatch.setenv("CC_LAUNCHER_DATA", str(tmp_path / "d"))
    env = default_env()
    assert (env.root, env.claude_home, env.qwen_home, env.data_dir) == (
        tmp_path / "r", tmp_path / "c", tmp_path / "q", tmp_path / "d")


def test_fake_env_layout(fake_env):
    assert (fake_env.root / "work" / "clients" / "bast" / "bifrost").is_dir()
    assert (fake_env.claude_home / "projects").is_dir() and (fake_env.qwen_home / "projects").is_dir()


def test_session_helpers_write_lf_only(fake_env):
    from launcher_helpers import write_claude_session, write_qwen_session
    cwd = fake_env.root / "work" / "net"
    c = write_claude_session(fake_env, cwd, "s1", [{"a": 1}, {"b": 2}])
    q = write_qwen_session(fake_env, cwd, "s2", [{"a": 1}])
    assert b"\r" not in c.read_bytes() and b"\r" not in q.read_bytes()
    assert q.with_name("s2.runtime.json").is_file()
