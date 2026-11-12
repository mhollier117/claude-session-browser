import dataclasses
import json
import os
import re
import threading
import time

import pytest

from launcher.index import Index
from launcher.scaffold import Result
from launcher.suggest import (UNREACHABLE_FILE, UNREACHABLE_TTL_S, Suggestion, Unreachable, default_http,
                              log_outcome, prepare, stage_claude, stage_history, stage_qwen, stage_root, suggest,
                              validate, validate_choice)
from launcher.suggest_prompt import SYSTEM_PROMPT
from launcher_helpers import make_row, write_claude_session

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


def _number(message, rel):
    """The number the user message gives the candidate `rel`."""
    m = re.search(rf"^(\d+)\. (?:PROJECT|PLACE)\s+{re.escape(rel)}(?:  |$)", message, re.M)
    assert m, f"{rel} not listed in:\n{message}"
    return int(m.group(1))


def _answer(rel, new_folder=None, confidence="high", reason="matched"):
    """A model answer choosing `rel`, worked out from the user message the model was sent."""
    return lambda message: json.dumps({"choice": _number(message, rel), "new_folder": new_folder,
                                       "confidence": confidence, "reason": reason})


def _content(content, message):
    return content(message) if callable(content) else content


class FakeHttp:
    """Records every request; answers /models with `models` and /chat/completions with `content` (a string, or a
    callable given the user message)."""

    def __init__(self, content, models=("qwen3-vl-huihui", "qwen3-vl-current"), delay=0.0, status=200):
        self.calls, self.content, self.models, self.delay, self.status = [], content, models, delay, status

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body, "timeout": timeout})
        if url.endswith("/models"):
            return 200, {"data": [{"id": m} for m in self.models]}
        time.sleep(self.delay)
        return self.status, {"choices": [{"message": {"content": _content(self.content,
                                                                          body["messages"][1]["content"])}}]}


class FakeRunner:
    def __init__(self, stdout="", code=0, before=None):
        self.calls, self.stdout, self.code, self.before = [], stdout, code, before

    def run(self, args, cwd, env=None, timeout=60, input=None):
        self.calls.append({"args": args, "cwd": cwd, "timeout": timeout, "env": env, "input": input})
        if self.before:
            self.before()
        out = self.stdout(input) if callable(self.stdout) else self.stdout
        return Result(self.code, out, "")


def _claude_out(inner, is_error=False):
    if callable(inner):
        return lambda message: json.dumps({"type": "result", "is_error": is_error, "result": inner(message)})
    return json.dumps({"type": "result", "is_error": is_error, "result": inner})


def _log(env):
    path = env.data_dir / "suggest.log"
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def _soon(seconds=5.0):
    return time.monotonic() + seconds


# ---- validate (path form) ------------------------------------------------------------------------------------------

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
    for null in ("null", " NULL ", "None", "none"):
        assert validate(fake_env, {"path": r"work\vendors", "new_folder": null}).new_folder is None, null
    assert validate(fake_env, ["not", "a", "dict"]) is None


def test_validate_accepts_case_insensitive_path_and_good_slug(fake_env):
    s = validate(fake_env, {"path": "WORK/Vendors", "new_folder": "keeper_sso", "confidence": "medium",
                            "reason": "chosen parent"}, source="qwen")
    assert s == Suggestion(fake_env.root / "work" / "vendors", "keeper_sso", "medium", "chosen parent", "qwen")
    assert s.alternatives == [] and s.strong is False
    s = validate(fake_env, {"path": str(fake_env.root / "work" / "net"), "new_folder": "", "confidence": "bogus"})
    assert s.path == fake_env.root / "work" / "net" and s.new_folder is None and s.confidence == "low"


# ---- validate_choice (v2) ------------------------------------------------------------------------------------------

def _prep_with_projects(fake_env):
    r, now = fake_env.root, fake_env.now()
    for rel in ("home/homelab/pxe", "home/homelab/pxe_old", "home/homelab/pxe_menu"):
        (r / rel).mkdir(parents=True)
    rows = [make_row(r / "home" / "homelab" / "pxe", "pxe boot order", now - 60, "s1"),
            make_row(r / "home" / "homelab" / "pxe_old", "pxe legacy", now - 90 * 86400, "s2"),
            make_row(r / "home" / "homelab" / "pxe_menu", "pxe menu", now - 120 * 86400, "s3")]
    return prepare(fake_env, rows, "pxe boot order")


def test_validate_choice_rejects_empty_out_of_range_and_non_int(fake_env):
    prep = _prep_with_projects(fake_env)
    n = len(prep.choices)
    for raw in ({}, {"choice": 0}, {"choice": n + 1}, {"choice": -1}, {"choice": "1"}, {"choice": 1.0},
                {"choice": True}, {"choice": None}, ["choice", 1], "1"):
        assert validate_choice(fake_env, raw, prep, "qwen") is None, raw


def test_validate_choice_project_forces_no_new_folder_and_sets_alternatives(fake_env):
    prep = _prep_with_projects(fake_env)
    r = fake_env.root
    pxe = _number(prep.message, "home\\homelab\\pxe")
    s = validate_choice(fake_env, {"choice": pxe, "new_folder": "something", "confidence": "high", "reason": " a  b "},
                        prep, "qwen")
    assert s.path == r / "home" / "homelab" / "pxe" and s.new_folder is None
    assert s.confidence == "high" and s.reason == "a b" and s.source == "qwen" and s.strong is False
    assert s.alternatives == [r / "home" / "homelab" / "pxe_old", r / "home" / "homelab" / "pxe_menu"]
    second = validate_choice(fake_env, {"choice": _number(prep.message, "home\\homelab\\pxe_old")}, prep, "qwen")
    assert second.alternatives == [r / "home" / "homelab" / "pxe", r / "home" / "homelab" / "pxe_menu"]
    assert second.confidence == "low"


def test_validate_choice_place_slugs_or_drops_the_new_folder(fake_env):
    prep = _prep_with_projects(fake_env)
    r = fake_env.root
    net = _number(prep.message, "work\\net")
    s = validate_choice(fake_env, {"choice": net, "new_folder": "FortiGate Cert", "confidence": "medium"}, prep, "qwen")
    assert s.path == r / "work" / "net" and s.new_folder == "fortigate_cert"
    assert s.needs_name is False
    for bad in (".hidden", "..", 7, "", "null", "None"):
        s = validate_choice(fake_env, {"choice": net, "new_folder": bad}, prep, "qwen")
        assert s is not None and s.path == r / "work" / "net" and s.new_folder is None, bad
        assert s.needs_name is True, bad  # no usable name, and work\net has no sessions of its own


def test_place_answer_naming_an_existing_child_leads_into_it(fake_env):
    prep = _prep_with_projects(fake_env)
    r = fake_env.root
    (r / "work" / "net" / "Taken_One").mkdir()
    net = _number(prep.message, "work\\net")
    s = validate_choice(fake_env, {"choice": net, "new_folder": "taken one", "confidence": "high"}, prep, "qwen")
    assert s.path == r / "work" / "net" / "Taken_One"  # the existing folder, on-disk spelling
    assert s.new_folder is None and s.needs_name is False


# ---- qwen ----------------------------------------------------------------------------------------------------------

def test_qwen_stage_picks_current_alias_and_sends_the_v2_prompt(fake_env, index):
    _qwen_settings(fake_env)
    http = FakeHttp(_answer(r"work\clients\bast\bifrost"))
    s = stage_qwen(http)(fake_env, index, "bifrost token refresh", _soon())
    assert s.source == "qwen" and s.path == fake_env.root / "work" / "clients" / "bast" / "bifrost"
    assert s.new_folder is None
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
    user = body["messages"][1]
    assert user["role"] == "user" and user["content"].startswith("Session name: bifrost token refresh\n")
    assert "1. PROJECT  work\\clients\\bast\\bifrost" in user["content"]
    assert chat["timeout"] <= 6
    log = _log(fake_env)
    assert log[-1]["stage"] == "qwen" and log[-1]["ok"] is True and log[-1]["prompt_chars"] > len(SYSTEM_PROMPT)
    assert set(log[-1]) >= {"stage", "name", "latency_ms", "ok", "reason_or_error", "prompt_chars"}
    assert _KEY not in (fake_env.data_dir / "suggest.log").read_text(encoding="utf-8")


def test_qwen_stage_falls_back_to_model_name_and_extracts_json_block(fake_env, index):
    _qwen_settings(fake_env)
    inner = _answer(r"work\net", "fortios_api", "medium")
    http = FakeHttp(lambda m: "Sure! " + inner(m) + " hope that helps {x}", models=("qwen3-vl-huihui", "other"))
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
        raise OSError(f"broken, header was {headers.get('Authorization')}")

    assert stage_qwen(boom)(fake_env, index, "vpn", _soon()) is None
    assert stage_qwen(FakeHttp("not json at all"))(fake_env, index, "vpn", _soon()) is None
    assert stage_qwen(FakeHttp("{}"))(fake_env, index, "vpn", _soon()) is None
    assert stage_qwen(FakeHttp('{"choice": 999}'))(fake_env, index, "vpn", _soon()) is None
    assert stage_qwen(FakeHttp(_answer(r"work\net"), status=500))(fake_env, index, "vpn", _soon()) is None
    assert _KEY not in (fake_env.data_dir / "suggest.log").read_text(encoding="utf-8")
    assert not (fake_env.data_dir / UNREACHABLE_FILE).exists()  # an error is not "unreachable"


def test_qwen_stage_falls_back_to_model_name_when_models_call_fails(fake_env, index):
    _qwen_settings(fake_env)
    inner = FakeHttp(_answer(r"work\net"))

    def http(method, url, headers, body, timeout):
        if url.endswith("/models"):
            raise OSError("models endpoint down")
        return inner(method, url, headers, body, timeout)

    s = stage_qwen(http)(fake_env, index, "vpn", _soon())
    assert s is not None and inner.calls[0]["body"]["model"] == "qwen3-vl-huihui"


@pytest.mark.parametrize("failure", [Unreachable("connect to qwen.test:30000 failed"), TimeoutError("timed out")])
def test_unreachable_qwen_is_remembered_for_ten_minutes(fake_env, index, failure):
    clock = [1_700_000_000.0]
    env = dataclasses.replace(fake_env, now=lambda: clock[0])
    _qwen_settings(env)
    calls = []

    def down(method, url, headers, body, timeout):
        calls.append(url)
        raise failure

    assert stage_qwen(down)(env, index, "vpn", _soon()) is None
    assert calls == ["http://qwen.test:30000/v1/models"]  # no chat call after the model list timed out
    assert float((env.data_dir / UNREACHABLE_FILE).read_text(encoding="utf-8")) == clock[0]
    clock[0] += UNREACHABLE_TTL_S - 1
    http = FakeHttp(_answer(r"work\net"))
    assert stage_qwen(http)(env, index, "vpn", _soon()) is None
    assert http.calls == []  # skipped without any network call
    assert _log(env)[-1]["reason_or_error"] == "skipped: unreachable (cached)"
    clock[0] += 2  # 600 s have passed: try again
    assert stage_qwen(http)(env, index, "vpn", _soon()) is not None and len(http.calls) == 2
    assert _KEY not in (env.data_dir / "suggest.log").read_text(encoding="utf-8")


def test_unreachable_on_the_chat_call_is_remembered_too(fake_env, index):
    _qwen_settings(fake_env)

    def http(method, url, headers, body, timeout):
        if url.endswith("/models"):
            return 200, {"data": [{"id": "x-current"}]}
        raise Unreachable("connect failed")

    assert stage_qwen(http)(fake_env, index, "vpn", _soon()) is None
    assert (fake_env.data_dir / UNREACHABLE_FILE).is_file()


def test_default_http_raises_unreachable_on_a_refused_connect():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens there now
    with pytest.raises(Unreachable):
        default_http("GET", f"http://127.0.0.1:{port}/v1/models", {}, None, 1)


# ---- claude --------------------------------------------------------------------------------------------------------

def test_claude_stage_parses_result_json(fake_env, index, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-leak")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-leak")
    monkeypatch.setenv("X_KEEP", "kept")
    exe = tmp_path / "claude.exe"
    exe.write_text("")
    runner = FakeRunner(_claude_out(_answer(r"home\ai", "ollama_bench", "medium", "local models")))
    s = stage_claude(runner, exe=exe)(fake_env, index, "ollama bench", _soon(30))
    assert s == Suggestion(fake_env.root / "home" / "ai", "ollama_bench", "medium", "local models", "claude")
    call = runner.calls[0]
    assert call["cwd"] == fake_env.data_dir / "suggest-cwd" and call["cwd"].is_dir()
    assert call["timeout"] <= 20
    args = call["args"]
    assert call["input"].startswith("Session name: ollama bench\n")  # the user message goes on stdin ...
    assert not any(call["input"].strip() in a for a in args)  # ... never into argv
    assert call["env"]["X_KEEP"] == "kept"
    assert "ANTHROPIC_API_KEY" not in call["env"] and "ANTHROPIC_AUTH_TOKEN" not in call["env"]
    assert args == [str(exe), "-p", "--model", "haiku", "--output-format", "json", "--system-prompt", SYSTEM_PROMPT,
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
    assert runner.calls == []


# ---- local stages ---------------------------------------------------------------------------------------------------

def test_history_stage_offers_the_top_ranked_folder(fake_env, index):
    r, now = fake_env.root, fake_env.now()
    target = r / "work" / "clients" / "bast" / "bifrost" / "stackjack_rip_qwen"
    target.mkdir()
    _session(fake_env, target, "s1", "rip the old client", now - 3600)
    _session(fake_env, target, "s2", "stackjack tool list", now - 7200)
    _session(fake_env, r / "work" / "net", "s3", "fix vpn phase", now - 60)
    index.refresh()
    s = stage_history(fake_env, index, "Stackjack phase 3", _soon())
    assert s.source == "history" and s.path == target and s.new_folder is None
    assert s.reason == "matches stackjack"
    assert s.strong is False  # one rare word only
    assert stage_history(fake_env, index, "new fix update the", _soon()) is None  # stop words only
    assert stage_history(fake_env, index, "zzzz", _soon()) is None  # nothing scores


def test_history_stage_strong_and_alternatives(fake_env, index):
    r, now = fake_env.root, fake_env.now()
    for name in ("pxe", "traefik", "supermicro"):
        (r / "home" / "homelab" / name).mkdir()
    for i in range(30):  # a realistic number of unrelated folders, so a word used in one branch is rare
        (r / "work" / "halo" / f"other{i:02d}").mkdir()
    _session(fake_env, r / "home" / "homelab" / "pxe", "s1", "pxe boot order", now - 60)
    _session(fake_env, r / "home" / "homelab" / "traefik", "s2", "traefik boot", now - 600)
    _session(fake_env, r / "home" / "homelab" / "supermicro", "s3", "supermicro ipmi", now - 900)
    index.refresh()
    s = stage_history(fake_env, index, "pxe boot order rework", _soon())
    assert s.path == r / "home" / "homelab" / "pxe" and s.reason == "matches pxe, boot, order"
    assert s.strong is True and s.confidence == "high"
    assert s.alternatives[0] == r / "home" / "homelab" / "traefik"
    weak = stage_history(fake_env, index, "boot", _soon())
    assert weak.strong is False and weak.confidence == "medium"


def test_root_stage(fake_env, index):
    s = stage_root(fake_env, index, "x", _soon())
    assert s.path == fake_env.root and s.source == "root" and s.new_folder is None and s.alternatives == []


# ---- suggest -------------------------------------------------------------------------------------------------------

def test_suggest_runs_claude_only_when_qwen_fails(fake_env, index, tmp_path):
    _qwen_settings(fake_env)
    exe = tmp_path / "claude.exe"
    exe.write_text("")
    updates = []
    runner = FakeRunner(_claude_out(_answer(r"home\ai")))
    http = FakeHttp(_answer(r"work\net", "vpn_tunnel", "high", "vpn work"))
    handle = suggest(fake_env, index, "fortigate vpn", http=http, runner=runner, on_update=updates.append,
                     claude_exe=exe)
    assert handle.local.source == "root"  # no session history: nothing scores
    got = handle.wait(5)
    assert handle.join(5)
    assert got.source == "qwen" and got.path == fake_env.root / "work" / "net" and got.new_folder == "vpn_tunnel"
    assert runner.calls == []  # Qwen answered: Claude never ran
    assert [u.source for u in updates] == ["qwen"]
    stages = [e["stage"] for e in _log(fake_env)]
    assert "keywords" not in stages and "claude" not in stages and {"history", "root", "qwen"} <= set(stages)

    updates.clear()
    handle = suggest(fake_env, index, "fortigate vpn", http=FakeHttp("{}"), runner=runner, on_update=updates.append,
                     claude_exe=exe)
    got = handle.wait(5)
    assert handle.join(5)
    assert got.source == "claude" and got.path == fake_env.root / "home" / "ai"
    assert len(runner.calls) == 1 and runner.calls[0]["input"].startswith("Session name: fortigate vpn\n")
    stages = [e["stage"] for e in _log(fake_env)]
    assert stages.index("claude") > len(stages) - 3  # after the failed qwen attempt


def test_suggest_stages_run_in_order_not_in_parallel(fake_env, index, tmp_path):
    _qwen_settings(fake_env)
    exe = tmp_path / "claude.exe"
    exe.write_text("")
    events = []

    class SlowFail(FakeHttp):
        def __call__(self, method, url, headers, body, timeout):
            if url.endswith("/chat/completions"):
                time.sleep(0.2)
                events.append("qwen done")
                return 500, "boom"
            return super().__call__(method, url, headers, body, timeout)

    runner = FakeRunner(_claude_out(_answer(r"home\ai")), before=lambda: events.append("claude start"))
    handle = suggest(fake_env, index, "vpn", http=SlowFail(""), runner=runner, on_update=lambda s: None,
                     claude_exe=exe)
    assert handle.join(5)
    assert events == ["qwen done", "claude start"]


def test_cancel_discards_an_answer_from_stages_already_running(fake_env, index, tmp_path):
    _qwen_settings(fake_env)
    exe = tmp_path / "claude.exe"
    exe.write_text("")
    updates, asked = [], threading.Event()
    release = threading.Event()

    class SlowHttp(FakeHttp):
        def __call__(self, method, url, headers, body, timeout):
            if url.endswith("/chat/completions"):
                asked.set()
                release.wait(5)
            return super().__call__(method, url, headers, body, timeout)

    runner = FakeRunner("", code=1)
    handle = suggest(fake_env, index, "fortigate vpn", http=SlowHttp("{}"), runner=runner,
                     on_update=updates.append, claude_exe=exe)
    assert asked.wait(5)  # the Qwen stage is waiting on its answer
    handle.cancel()
    release.set()
    assert handle.join(5)
    assert handle.wait(0) is None and updates == []
    assert runner.calls == []  # cancelled: Claude is not started after Qwen failed


def test_suggest_never_hangs_on_bad_model_answers(fake_env, index, tmp_path):
    _qwen_settings(fake_env)
    exe = tmp_path / "claude.exe"
    exe.write_text("")
    updates = []

    def bad_update(s):
        updates.append(s)
        raise RuntimeError("ui gone")

    http = FakeHttp('{"choice": 0}')
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


def test_default_http_does_not_follow_redirects():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    seen = []

    class Quiet(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

    class Target(Quiet):
        def do_GET(self):
            seen.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    target = ThreadingHTTPServer(("127.0.0.1", 0), Target)

    class Redirect(Quiet):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target.server_port}/stolen")
            self.send_header("Content-Length", "0")
            self.end_headers()

    redirect = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    servers = [target, redirect]
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for t in threads:
        t.start()
    try:
        status, _ = default_http("GET", f"http://127.0.0.1:{redirect.server_port}/v1/models",
                                 {"Authorization": "Bearer secret"}, None, 5)
    finally:
        for s in servers:
            s.shutdown()
            s.server_close()
        for t in threads:
            t.join(5)
    assert status == 302 and seen == []  # the key never reached the redirect target


class _ListIndex:
    """An index stand-in serving a fixed row list; records which threads read it."""

    def __init__(self, rows):
        self._rows, self.readers = rows, []

    def rows(self, filter_text=""):
        self.readers.append(threading.get_ident())
        return self._rows


def test_suggest_returns_fast_on_a_large_tree(fake_env, tmp_path):
    _qwen_settings(fake_env)
    r = fake_env.root
    folders = []
    for c in range(30):
        for p in range(9):
            folder = r / "work" / "clients" / f"client{c:02d}" / f"proj{p}"
            folder.mkdir(parents=True)
            folders.append(folder)
    rows = [make_row(folders[i % len(folders)], f"session {i} about widget{i % 50}", 1_699_000_000.0 + i, f"s{i}")
            for i in range(2000)]
    index = _ListIndex(rows)
    built = []
    t0 = time.monotonic()
    handle = suggest(fake_env, index, "widget7 rollout", http=FakeHttp("not json"), runner=FakeRunner(""),
                     on_update=built.append, claude_exe=tmp_path / "missing.exe")
    elapsed = time.monotonic() - t0
    assert handle.join(10)
    assert elapsed < 2.0, elapsed
    assert handle.local.source == "history" and "widget7" in handle.local.reason
    assert index.readers == [threading.get_ident()]  # rows read once, on the calling thread
    qwen = next(e for e in _log(fake_env) if e["stage"] == "qwen")
    assert len(SYSTEM_PROMPT) < qwen["prompt_chars"] < len(SYSTEM_PROMPT) + 4000  # a shortlist, not the tree


def test_suggest_survives_an_index_that_raises(fake_env, tmp_path):
    class Broken:
        def rows(self, filter_text=""):
            raise RuntimeError("db locked")

    handle = suggest(fake_env, Broken(), "fortigate vpn", http=FakeHttp("x"), runner=FakeRunner(""),
                     on_update=lambda s: None, claude_exe=tmp_path / "missing.exe")
    assert handle.local.source == "root" and handle.join(5)
    assert any(e["stage"] == "index" and e["ok"] is False and "db locked" in e["reason_or_error"]
               for e in _log(fake_env))


# ---- outcome log ---------------------------------------------------------------------------------------------------

def test_log_outcome_line_shape(fake_env):
    r = fake_env.root
    s = Suggestion(r / "work" / "net", "vpn_tunnel", "high", "x", "qwen")
    log_outcome(fake_env, "fortigate vpn", s, r / "work" / "net", "vpn_tunnel")
    log_outcome(fake_env, "fortigate vpn", s, r / "work" / "halo", None)
    log_outcome(fake_env, "zzz", None, r / "home", "zzz")
    root = Suggestion(r, None, "low", "no suggestion", "root")
    log_outcome(fake_env, "zzz", root, r / "home", None)
    a, b, c, d = _log(fake_env)
    assert a == {"ts": fake_env.now(), "stage": "outcome", "name": "fortigate vpn", "suggested": "work\\net",
                 "suggested_source": "qwen", "chosen": "work\\net", "new_folder": "vpn_tunnel", "match": True}
    assert b["chosen"] == "work\\halo" and b["new_folder"] is None and b["match"] is False
    assert c["suggested"] is None and c["suggested_source"] is None and c["match"] is False
    assert d["suggested"] is None and d["suggested_source"] == "root" and d["match"] is False
    s2 = Suggestion(r / "work" / "net", None, "high", "x", "history")
    log_outcome(fake_env, "n", s2, r / "WORK" / "Net", None)
    assert _log(fake_env)[-1]["match"] is True  # case-insensitive
    log_outcome(fake_env, "n", s2, r / "work" / "net", "sub")
    assert _log(fake_env)[-1]["match"] is False  # a new folder below the suggested project is not a match


def test_place_with_null_new_folder_is_guarded_by_its_own_sessions(fake_env):
    r, now = fake_env.root, fake_env.now()
    rows = [make_row(r / "home" / "ai", "whisper bench", now - 60, "s1")]
    prep = prepare(fake_env, rows, "whisper bench rerun")
    ai, net = _number(prep.message, "home\\ai"), _number(prep.message, "work\\net")
    s = validate_choice(fake_env, {"choice": ai, "new_folder": None, "confidence": "high"}, prep, "qwen")
    assert s.path == r / "home" / "ai" and s.new_folder is None and s.needs_name is False  # start in the place
    s = validate_choice(fake_env, {"choice": net, "new_folder": None, "confidence": "high"}, prep, "qwen")
    assert s is not None and s.path == r / "work" / "net" and s.new_folder is None
    assert s.needs_name is True  # no sessions in work\net itself: the UI asks for a folder name


def test_unwritable_data_dir_never_raises(fake_env, index):
    from launcher.suggest import _log, _remember_unreachable
    blocker = fake_env.root.parent / "not_a_dir"
    blocker.write_text("a file, not a folder", encoding="utf-8")
    env = dataclasses.replace(fake_env, data_dir=blocker / "data")  # every write under it fails
    _log(env, {"stage": "x"})
    _remember_unreachable(env)
    _qwen_settings(env)

    def down(method, url, headers, body, timeout):
        raise Unreachable("connect failed")

    assert stage_qwen(down)(env, index, "vpn", _soon()) is None
    assert not (blocker / "data").exists()


def test_a_place_answer_without_a_name_does_not_fall_through_to_claude(fake_env, index, tmp_path):
    _qwen_settings(fake_env)
    exe = tmp_path / "claude.exe"
    exe.write_text("")
    runner = FakeRunner(_claude_out(_answer(r"home\ai")))
    handle = suggest(fake_env, index, "zzz qqq", http=FakeHttp(_answer(r"work\net", None, "medium")), runner=runner,
                     on_update=lambda s: None, claude_exe=exe)
    got = handle.wait(5)
    assert handle.join(5)
    assert got.source == "qwen" and got.path == fake_env.root / "work" / "net" and got.needs_name is True
    assert runner.calls == []
