import json
import os
import threading
import time

import pytest

from launcher.index import Index
from launcher.scaffold import Result
from launcher.suggest import (KEYWORDS, Suggestion, stage_claude, stage_history, stage_keywords, stage_qwen,
                              stage_root, suggest, validate)
from launcher.suggest_prompt import SYSTEM_PROMPT
from launcher_helpers import write_claude_session

_KEY = "sk-test-SECRET-123"
_ENV_KEY = "QWEN_KEY_TEST"


def _session(env, cwd, sid, name, mtime):
    path = write_claude_session(env, cwd, sid, [
        {"type": "user", "cwd": str(cwd), "timestamp": "2026-11-01T10:00:00Z",
         "message": {"role": "user", "content": name}}])
    os.utime(path, (mtime, mtime))


@pytest.fixture
def index(fake_env):
    idx = Index(fake_env, claude_live=lambda: [])
    yield idx
    idx.close()


def _qwen_settings(env, name="qwen3-vl-huihui"):
    env.qwen_home.mkdir(parents=True, exist_ok=True)
    (env.qwen_home / "settings.json").write_text(json.dumps({
        "model": {"name": name, "baseUrl": "http://qwen.test:30000/v1"},
        "modelProviders": {"openai": [
            {"id": "qwen3-vl-base", "envKey": "OTHER_KEY"},
            {"id": name, "envKey": _ENV_KEY, "baseUrl": "http://qwen.test:30000/v1"}]},
        "env": {_ENV_KEY: _KEY, "OTHER_KEY": "nope"},
    }), encoding="utf-8")


def _answer(path, new_folder=None, confidence="high", reason="matched"):
    return json.dumps({"path": path, "new_folder": new_folder, "confidence": confidence, "reason": reason})


class FakeHttp:
    """Records every request; answers /models with `models` and /chat/completions with `content`."""

    def __init__(self, content, models=("qwen3-vl-huihui", "qwen3-vl-current"), delay=0.0, status=200):
        self.calls, self.content, self.models, self.delay, self.status = [], content, models, delay, status

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body, "timeout": timeout})
        if url.endswith("/models"):
            return 200, {"data": [{"id": m} for m in self.models]}
        time.sleep(self.delay)
        return self.status, {"choices": [{"message": {"content": self.content}}]}


class FakeRunner:
    def __init__(self, stdout="", code=0, before=None):
        self.calls, self.stdout, self.code, self.before = [], stdout, code, before

    def run(self, args, cwd, env=None, timeout=60):
        self.calls.append({"args": args, "cwd": cwd, "timeout": timeout})
        if self.before:
            self.before()
        return Result(self.code, self.stdout, "")


def _claude_out(inner, is_error=False):
    return json.dumps({"type": "result", "is_error": is_error, "result": inner})


def _log(env):
    path = env.data_dir / "suggest.log"
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def _soon(seconds=5.0):
    return time.monotonic() + seconds


# ---- validate ------------------------------------------------------------------------------------------------------

def test_validate_rejects_missing_path_and_bad_slug(fake_env):
    assert validate(fake_env, {"path": r"work\vendors\keeper", "new_folder": None, "confidence": "high",
                               "reason": "x"}) is None
    assert validate(fake_env, {"path": r"work\vendors", "new_folder": "Keeper SSO", "confidence": "medium",
                               "reason": "x"}) is None
    for bad in ("..", ".hidden", "a:b", "x*", "a/b", 7):
        assert validate(fake_env, {"path": r"work\vendors", "new_folder": bad, "confidence": "low",
                                   "reason": "x"}) is None, bad
    assert validate(fake_env, {"path": ".", "new_folder": "x"}) is None  # the root itself
    assert validate(fake_env, {"path": r"..\outside"}) is None
    assert validate(fake_env, {"path": None}) is None
    assert validate(fake_env, ["not", "a", "dict"]) is None


def test_validate_accepts_case_insensitive_path_and_good_slug(fake_env):
    s = validate(fake_env, {"path": "WORK/Vendors", "new_folder": "keeper_sso", "confidence": "medium",
                            "reason": "chosen parent"}, source="qwen")
    assert s == Suggestion(fake_env.root / "work" / "vendors", "keeper_sso", "medium", "chosen parent", "qwen")
    s = validate(fake_env, {"path": str(fake_env.root / "work" / "net"), "new_folder": "", "confidence": "bogus"})
    assert s.path == fake_env.root / "work" / "net" and s.new_folder is None and s.confidence == "low"


# ---- qwen ----------------------------------------------------------------------------------------------------------

def test_qwen_stage_picks_current_alias_and_thinking_off(fake_env, index):
    _qwen_settings(fake_env)
    http = FakeHttp(_answer(r"work\clients\bast\bifrost"))
    s = stage_qwen(http)(fake_env, index, "bifrost token refresh", _soon())
    assert s.source == "qwen" and s.path == fake_env.root / "work" / "clients" / "bast" / "bifrost"
    models, chat = http.calls
    assert models["method"] == "GET" and models["url"] == "http://qwen.test:30000/v1/models"
    assert models["timeout"] <= 2
    assert chat["method"] == "POST" and chat["url"] == "http://qwen.test:30000/v1/chat/completions"
    assert chat["headers"]["Authorization"] == f"Bearer {_KEY}"
    body = chat["body"]
    assert body["model"] == "qwen3-vl-current"
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["temperature"] == 0 and body["max_tokens"] == 400
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"][0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert body["messages"][1]["role"] == "user"
    assert body["messages"][1]["content"].startswith("Session name: bifrost token refresh\n")
    assert chat["timeout"] <= 6
    log = _log(fake_env)
    assert log[-1]["stage"] == "qwen" and log[-1]["ok"] is True and log[-1]["prompt_chars"] > len(SYSTEM_PROMPT)
    assert set(log[-1]) >= {"stage", "name", "latency_ms", "ok", "reason_or_error", "prompt_chars"}
    assert _KEY not in (fake_env.data_dir / "suggest.log").read_text(encoding="utf-8")


def test_qwen_stage_falls_back_to_model_name_and_extracts_json_block(fake_env, index):
    _qwen_settings(fake_env)
    http = FakeHttp("Sure! " + _answer(r"work\net", "fortios_api", "medium") + " hope that helps {x}",
                    models=("qwen3-vl-huihui", "other"))
    s = stage_qwen(http)(fake_env, index, "FortiOS API", _soon())
    assert http.calls[1]["body"]["model"] == "qwen3-vl-huihui"
    assert s.path == fake_env.root / "work" / "net" and s.new_folder == "fortios_api"


def test_qwen_stage_times_out_silently(fake_env, index):
    _qwen_settings(fake_env)
    http = FakeHttp(_answer(r"work\net"), delay=0.3)
    assert stage_qwen(http)(fake_env, index, "vpn", time.monotonic() + 0.1) is None
    entry = _log(fake_env)[-1]
    assert entry["ok"] is False and "deadline" in entry["reason_or_error"]


def test_qwen_stage_skips_without_settings_and_on_errors(fake_env, index):
    http = FakeHttp(_answer(r"work\net"))
    assert stage_qwen(http)(fake_env, index, "vpn", _soon()) is None  # no settings.json
    assert http.calls == []
    _qwen_settings(fake_env)

    def boom(method, url, headers, body, timeout):
        raise OSError(f"unreachable, header was {headers.get('Authorization')}")

    assert stage_qwen(boom)(fake_env, index, "vpn", _soon()) is None
    assert stage_qwen(FakeHttp("not json at all"))(fake_env, index, "vpn", _soon()) is None
    assert stage_qwen(FakeHttp(_answer(r"work\vendors\keeper")))(fake_env, index, "keeper", _soon()) is None
    assert stage_qwen(FakeHttp(_answer(r"work\net"), status=500))(fake_env, index, "vpn", _soon()) is None
    assert _KEY not in (fake_env.data_dir / "suggest.log").read_text(encoding="utf-8")


# ---- claude --------------------------------------------------------------------------------------------------------

def test_claude_stage_parses_result_json(fake_env, index, tmp_path):
    exe = tmp_path / "claude.exe"
    exe.write_text("")
    runner = FakeRunner(_claude_out(_answer(r"home\ai", "ollama_bench", "medium", "local models")))
    s = stage_claude(runner, exe=exe)(fake_env, index, "ollama bench", _soon(30))
    assert s == Suggestion(fake_env.root / "home" / "ai", "ollama_bench", "medium", "local models", "claude")
    call = runner.calls[0]
    assert call["cwd"] == fake_env.data_dir / "suggest-cwd" and call["cwd"].is_dir()
    assert call["timeout"] <= 20
    args = call["args"]
    assert args[0] == str(exe) and args[1] == "-p" and args[2].startswith("Session name: ollama bench\n")
    assert args[3:] == ["--model", "haiku", "--output-format", "json", "--system-prompt", SYSTEM_PROMPT,
                        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--tools", "",
                        "--no-session-persistence"]
    assert _log(fake_env)[-1]["stage"] == "claude" and _log(fake_env)[-1]["ok"] is True


def test_claude_stage_skips_on_error_exit_or_missing_exe(fake_env, index, tmp_path):
    exe = tmp_path / "claude.exe"
    exe.write_text("")
    good = _answer(r"home\ai")
    assert stage_claude(FakeRunner(_claude_out(good, is_error=True)), exe=exe)(fake_env, index, "x", _soon()) is None
    assert stage_claude(FakeRunner(_claude_out(good), code=1), exe=exe)(fake_env, index, "x", _soon()) is None
    assert stage_claude(FakeRunner("not json"), exe=exe)(fake_env, index, "x", _soon()) is None
    runner = FakeRunner(_claude_out(good))
    assert stage_claude(runner, exe=tmp_path / "missing.exe")(fake_env, index, "x", _soon()) is None
    assert stage_claude(runner, exe=exe, message="x" * 40_000)(fake_env, index, "x", _soon()) is None
    assert runner.calls == []
    assert "Windows limit" in _log(fake_env)[-1]["reason_or_error"]


# ---- local stages ---------------------------------------------------------------------------------------------------

def test_history_stage_matches_word_overlap(fake_env, index):
    r, now = fake_env.root, fake_env.now()
    target = r / "work" / "clients" / "bast" / "bifrost" / "stackjack_rip_qwen"
    target.mkdir()
    _session(fake_env, target, "s1", "rip the old client", now - 3600)
    _session(fake_env, target, "s2", "stackjack tool list", now - 7200)
    _session(fake_env, r / "work" / "net", "s3", "fix vpn phase", now - 60)
    index.refresh()
    s = stage_history(fake_env, index, "Stackjack phase 3", _soon())
    assert s.source == "history" and s.path == target and s.new_folder is None
    assert s.reason == "2 similar sessions live here"
    assert stage_history(fake_env, index, "new fix update the", _soon()) is None  # stop words only


def test_history_stage_tie_goes_to_most_recent(fake_env, index):
    r, now = fake_env.root, fake_env.now()
    _session(fake_env, r / "work" / "net", "s1", "keeper vault", now - 7200)
    _session(fake_env, r / "work" / "vendors", "s2", "keeper sso", now - 60)
    index.refresh()
    assert stage_history(fake_env, index, "keeper", _soon()).path == r / "work" / "vendors"


def test_keywords_stage_bucket_only(fake_env, index):
    s = stage_keywords(fake_env, index, "FortiGate VPN tunnel", _soon())
    assert s.path == fake_env.root / "work" / "net" and s.new_folder is None and s.source == "keywords"
    assert stage_keywords(fake_env, index, "something unrelated", _soon()) is None
    assert stage_keywords(fake_env, index, "rewst jinja", _soon()) is None  # bucket missing on disk
    assert KEYWORDS["work\\net"][:3] == ("forti", "fortigate", "fortios")


def test_root_stage(fake_env, index):
    s = stage_root(fake_env, index, "x", _soon())
    assert s.path == fake_env.root and s.source == "root" and s.new_folder is None


# ---- suggest -------------------------------------------------------------------------------------------------------

def test_suggest_returns_local_first_then_updates_once(fake_env, index, tmp_path):
    _qwen_settings(fake_env)
    exe = tmp_path / "claude.exe"
    exe.write_text("")
    updates, first = [], threading.Event()

    def on_update(s):
        updates.append(s)
        first.set()

    runner = FakeRunner(_claude_out(_answer(r"home\ai")), before=lambda: first.wait(5))
    http = FakeHttp(_answer(r"work\net", None, "high", "vpn work"))
    handle = suggest(fake_env, index, "fortigate vpn", http=http, runner=runner, on_update=on_update,
                     claude_exe=exe)
    assert handle.local.source == "keywords" and handle.local.path == fake_env.root / "work" / "net"
    got = handle.wait(5)
    assert got.source == "qwen"
    assert handle.join(5)
    assert len(runner.calls) == 1  # Claude did answer, after Qwen ...
    assert [u.source for u in updates] == ["qwen"]  # ... and was discarded
    stages = [e["stage"] for e in _log(fake_env)]
    assert {"history", "keywords", "qwen", "claude"} <= set(stages)


def test_suggest_never_hangs_on_bad_model_answers(fake_env, index, tmp_path):
    _qwen_settings(fake_env)
    exe = tmp_path / "claude.exe"
    exe.write_text("")
    updates = []

    def bad_update(s):
        updates.append(s)
        raise RuntimeError("ui gone")

    http = FakeHttp(_answer(r"work\vendors\keeper"))  # folder does not exist
    runner = FakeRunner("garbage")
    t0 = time.monotonic()
    handle = suggest(fake_env, index, "nothing matches", http=http, runner=runner, on_update=bad_update,
                     claude_exe=exe)
    assert time.monotonic() - t0 < 1
    assert handle.local.source == "root"
    assert handle.wait(5) is None
    assert handle.join(5) and updates == []


def test_suggest_survives_raising_on_update(fake_env, index, tmp_path):
    _qwen_settings(fake_env)
    seen = []

    def bad_update(s):
        seen.append(s)
        raise RuntimeError("ui gone")

    handle = suggest(fake_env, index, "vpn", http=FakeHttp(_answer(r"work\net")), runner=FakeRunner(""),
                     on_update=bad_update, claude_exe=tmp_path / "missing.exe")
    assert handle.wait(5).source == "qwen"
    assert handle.join(5) and len(seen) == 1
    assert any(e["stage"] == "on_update" and e["ok"] is False for e in _log(fake_env))
