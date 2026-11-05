"""Where should a new session live? Local answers (history, keywords, root) come back at once; the self-hosted Qwen
endpoint and Claude Code (Haiku) run concurrently in the background and the first valid model answer is offered once.

The picker never waits on the model stages: `suggest()` returns the local answer immediately; Qwen has 6 s in total
(1 s TCP connect, 2 s for the model list) and Claude 20 s, each enforced inside its own stage. Every stage attempt is
appended to `data_dir/suggest.log` (stage, name, latency, outcome, prompt size; never the API key or the prompt)."""
from __future__ import annotations

import json
import re
import shutil
import os
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .index import Index
from .paths import Env
from .scaffold import Runner, _check_name
from .suggest_prompt import SYSTEM_PROMPT, build_user_message
from .tree import slug

QWEN_TOTAL_S = 6.0
QWEN_CONNECT_S = 1.0
QWEN_MODELS_S = 2.0
CLAUDE_TOTAL_S = 20.0

# Copied from cc\launcher-spike\cc-new-spike.ps1 ($keywords). Matched as whole words of the lower-cased name.
KEYWORDS: dict[str, tuple[str, ...]] = {
    "work\\net": ("forti", "fortigate", "fortios", "vpn", "ikev2", "cert", "dns", "firewall", "acme"),
    "work\\halo": ("halo", "psa", "ticket", "runbook"),
    "work\\rewst": ("rewst", "workflow", "jinja", "graphql"),
    "work\\ninja": ("ninja", "rmm", "script", "deploy"),
    "work\\cipp": ("cipp", "standards"),
    "work\\m365": ("m365", "entra", "intune", "exchange", "teams", "sharepoint", "ca policy"),
    "work\\vendors": ("huntress", "keeper", "keepit", "vircom", "blackpoint", "proofpoint", "screenconnect"),
    "home\\homelab": ("homelab", "pxe", "supermicro", "fgt2", "traefik"),
    "home\\ai": ("qwen", "ollama", "llama", "model"),
    "cc": ("claude", "session", "launcher", "hook", "skill", "reorg"),
}

_STOP = {"new", "fix", "phase", "update", "the", "and", "for", "with"}
_CONFIDENCE = ("high", "medium", "low")
_NULLS = ("", "null", "none")
_CLAUDE_ENV_DROP = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")  # use the subscription login, never a key
_ANSWER_LOG_CHARS = 1000


@dataclass
class Suggestion:
    path: Path  # existing folder under env.root (on-disk spelling); env.root itself only for source "root"
    new_folder: str | None
    confidence: str  # high | medium | low
    reason: str
    source: str  # qwen | claude | history | keywords | root


Http = Callable[[str, str, dict, "dict | None", float], "tuple[int, dict | str]"]
Stage = Callable[[Env, Index, str, float], "Suggestion | None"]


class _Skip(Exception):
    """A stage has nothing to offer; the message goes to the log."""


# ---- logging -------------------------------------------------------------------------------------------------------

_log_lock = threading.Lock()


def _log(env: Env, entry: dict) -> None:
    try:
        env.data_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with _log_lock, open(env.data_dir / "suggest.log", "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass  # the log is for tuning only


def _attempt(env: Env, stage: str, name: str, body: Callable[[dict], Suggestion]) -> Suggestion | None:
    """Run one stage body, never raising: a `_Skip` or any error becomes None. Logs one line either way.
    `body` may set info["prompt_chars"], info["answer"] (raw model text) and info["secret"] (redacted from the log)."""
    info: dict = {"prompt_chars": None, "answer": None, "secret": None}
    t0 = time.monotonic()
    result = None
    try:
        result = body(info)
        why = result.reason
    except _Skip as e:
        why = str(e)
    except Exception as e:  # noqa: BLE001 - a suggestion stage must never break the picker
        why = f"{type(e).__name__}: {e}"
    entry = {"ts": env.now(), "stage": stage, "name": name, "latency_ms": round((time.monotonic() - t0) * 1000),
             "ok": result is not None, "reason_or_error": why, "prompt_chars": info["prompt_chars"]}
    if isinstance(info["answer"], str):
        entry["answer"] = info["answer"][:_ANSWER_LOG_CHARS]
    if info["secret"]:
        entry = {k: v.replace(info["secret"], "***") if isinstance(v, str) else v for k, v in entry.items()}
    _log(env, entry)
    return result


# ---- validation ----------------------------------------------------------------------------------------------------

def _resolve(root: Path, value) -> Path | None:
    """The existing directory strictly under `root` named by `value` (relative, or absolute under root), matched
    case-insensitively one component at a time; None otherwise."""
    if not isinstance(value, str) or not value.strip():
        return None
    p = Path(value.strip())
    pieces = p.parts
    if p.anchor:
        if [x.lower() for x in pieces[:len(root.parts)]] != [x.lower() for x in root.parts]:
            return None
        pieces = pieces[len(root.parts):]
    pieces = [x for x in pieces if x != "."]
    if not pieces or ".." in pieces:
        return None
    cur = root
    for piece in pieces:
        try:
            dirs = [c for c in cur.iterdir() if c.is_dir()]
        except OSError:
            return None
        cur = next((c for c in dirs if c.name == piece), None) or \
            next((c for c in dirs if c.name.lower() == piece.lower()), None)
        if cur is None:
            return None
    return cur


def validate(env: Env, raw, source: str = "") -> Suggestion | None:
    """A model-shaped answer as a Suggestion, or None when `path` is not an existing folder under the root (the root
    itself excluded) or `new_folder` is not already a slug that is safe to create."""
    if not isinstance(raw, dict):
        return None
    path = _resolve(env.root, raw.get("path"))
    if path is None:
        return None
    new_folder = raw.get("new_folder")
    if isinstance(new_folder, str) and new_folder.strip().lower() in _NULLS:
        new_folder = None  # models copy the schema's "<name or null>" literally
    if new_folder is not None:
        if not isinstance(new_folder, str) or slug(new_folder) != new_folder:
            return None
        try:
            _check_name(new_folder)
        except ValueError:
            return None
    confidence = raw.get("confidence") if raw.get("confidence") in _CONFIDENCE else "low"
    reason = " ".join(raw["reason"].split()) if isinstance(raw.get("reason"), str) else ""
    return Suggestion(path, new_folder, confidence, reason, source)


def _parse_json(text) -> dict | None:
    """`text` as a JSON object, else the first `{...}` block inside it that parses as one."""
    if not isinstance(text, str):
        return None
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            value, _ = decoder.raw_decode(text, m.start())
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _model_answer(env: Env, text, source: str) -> Suggestion:
    raw = _parse_json(text)
    if raw is None:
        raise _Skip("answer is not JSON")
    s = validate(env, raw, source=source)
    if s is None:
        raise _Skip(f"answer failed validation: path={raw.get('path')!r} new_folder={raw.get('new_folder')!r}")
    return s


def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise _Skip("deadline passed")
    return left


# ---- model stages ---------------------------------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx is returned as an error status instead of being followed (urllib would resend Authorization)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def default_http(method: str, url: str, headers: dict, body: dict | None, timeout: float) -> tuple[int, dict | str]:
    """One HTTP call: a TCP connect check (at most 1 s) first, then the request without any proxy (the endpoint is
    on the LAN/VPN) and without following redirects. Returns (status, parsed JSON or text); network errors raise."""
    parts = urllib.parse.urlsplit(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    with socket.create_connection((parts.hostname, port), timeout=min(QWEN_CONNECT_S, timeout)):
        pass
    data = None if body is None else json.dumps(body).encode("utf-8")
    hdrs = dict(headers)
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        with e:
            status, raw = e.code, e.read()
    text = raw.decode("utf-8", "replace")
    try:
        return status, json.loads(text)
    except ValueError:
        return status, text


def _qwen_config(env: Env) -> tuple[str, str, str]:
    """(api key, base URL ending in /v1, configured model name) from qwen_home/settings.json."""
    try:
        settings = json.loads((env.qwen_home / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise _Skip("qwen settings missing or unreadable") from None
    model = settings.get("model") if isinstance(settings, dict) else None
    model = model if isinstance(model, dict) else {}
    name, base = model.get("name"), model.get("baseUrl")
    providers = (settings.get("modelProviders") or {}).get("openai") or []
    entry = next((p for p in providers if isinstance(p, dict) and p.get("id") == name), {})
    env_key = entry.get("envKey")
    key = (settings.get("env") or {}).get(env_key) if isinstance(env_key, str) else None
    if not all(isinstance(v, str) and v for v in (name, base, key)):
        raise _Skip("qwen settings incomplete (model.name, model.baseUrl, provider envKey or env key)")
    base = base.rstrip("/")
    return key, base if base.endswith("/v1") else base + "/v1", name


def _current_alias(data) -> str | None:
    items = data.get("data") if isinstance(data, dict) else None
    ids = [m.get("id") for m in items if isinstance(m, dict)] if isinstance(items, list) else []
    return next((i for i in ids if isinstance(i, str) and i.endswith("-current")), None)


def stage_qwen(http: Http = default_http, message: str | None = None) -> Stage:
    """Stage 1: the self-hosted Qwen endpoint. `message` is the prebuilt user message (built from the index when
    None, on the calling thread)."""

    def run(env: Env, index: Index, name: str, deadline: float) -> Suggestion | None:
        def body(info: dict) -> Suggestion:
            end = min(deadline, time.monotonic() + QWEN_TOTAL_S)
            key, base, configured = _qwen_config(env)
            info["secret"] = key
            msg = message if message is not None else build_user_message(env, index, name)
            info["prompt_chars"] = len(SYSTEM_PROMPT) + len(msg)
            headers = {"Authorization": f"Bearer {key}"}
            try:
                status, data = http("GET", base + "/models", headers, None, min(QWEN_MODELS_S, _remaining(end)))
                alias = _current_alias(data) if status == 200 else None
            except _Skip:
                raise
            except Exception:  # noqa: BLE001 - no model list: fall back to the configured name
                alias = None
            model = alias or configured
            request = {
                "model": model,
                "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": msg}],
                "chat_template_kwargs": {"enable_thinking": False},
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "max_tokens": 400,
            }
            status, data = http("POST", base + "/chat/completions", headers, request, _remaining(end))
            _remaining(end)  # an answer that arrives after the deadline is dropped
            if status != 200:
                raise _Skip(f"HTTP {status}")
            try:
                content = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                raise _Skip("no choices[0].message.content in the response") from None
            info["answer"] = content
            return _model_answer(env, content, "qwen")

        return _attempt(env, "qwen", name, body)

    return run


def _find_claude() -> Path | None:
    found = shutil.which("claude.exe")
    if found:
        return Path(found)
    fallback = Path.home() / ".local" / "bin" / "claude.exe"
    return fallback if fallback.is_file() else None


def stage_claude(runner: Runner | None = None, exe: Path | None = None, message: str | None = None) -> Stage:
    """Stage 2: Haiku through the signed-in Claude Code CLI (`claude.exe -p`), run from `data_dir/suggest-cwd`.
    `exe` overrides the PATH / ~/.local/bin lookup; `message` as in `stage_qwen`."""
    runner = runner or Runner()

    def run(env: Env, index: Index, name: str, deadline: float) -> Suggestion | None:
        def body(info: dict) -> Suggestion:
            end = min(deadline, time.monotonic() + CLAUDE_TOTAL_S)
            binary = Path(exe) if exe is not None else _find_claude()
            if binary is None or not binary.is_file():
                raise _Skip("claude.exe not found")
            msg = message if message is not None else build_user_message(env, index, name)
            info["prompt_chars"] = len(SYSTEM_PROMPT) + len(msg)
            cwd = env.data_dir / "suggest-cwd"
            cwd.mkdir(parents=True, exist_ok=True)
            # the user message goes on stdin (print mode reads the prompt from it): no command-line length limit
            args = [str(binary), "-p", "--model", "haiku", "--output-format", "json",
                    "--system-prompt", SYSTEM_PROMPT, "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                    "--tools", "", "--no-session-persistence"]
            env_ = {k: v for k, v in os.environ.items() if k.upper() not in _CLAUDE_ENV_DROP}
            res = runner.run(args, cwd, env=env_, timeout=_remaining(end), input=msg)
            if res.code != 0:
                raise _Skip(f"claude exited {res.code}: {res.err.strip()[:200]}")
            try:
                out = json.loads(res.out)
            except ValueError:
                raise _Skip("claude output is not JSON") from None
            if not isinstance(out, dict) or out.get("is_error"):
                raise _Skip("claude reported is_error")
            info["answer"] = out.get("result")
            return _model_answer(env, out.get("result"), "claude")

        return _attempt(env, "claude", name, body)

    return run


# ---- local stages ---------------------------------------------------------------------------------------------------

def _tokens(text: str | None) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if len(t) >= 3 and t not in _STOP}


def stage_history(env: Env, index: Index, name: str, deadline: float) -> Suggestion | None:
    """Stage 3: the folder whose sessions share the most words with the name (session names + folder name)."""

    def body(info: dict) -> Suggestion:
        want = _tokens(name)
        if not want:
            raise _Skip("no distinctive words in the name")
        groups: dict[str, list] = {}  # folder key -> [cwd, total score, sessions, last activity]
        for r in index.rows():
            if not r.cwd:
                continue
            score = len(want & (_tokens(r.display_name) | _tokens(Path(r.cwd).name)))
            if score:
                g = groups.setdefault(str(Path(r.cwd)).lower(), [r.cwd, 0, 0, 0.0])
                g[1] += score
                g[2] += 1
                g[3] = max(g[3], r.last_activity)
        for cwd, _total, n, _last in sorted(groups.values(), key=lambda g: (-g[1], -g[3])):
            reason = "1 similar session lives here" if n == 1 else f"{n} similar sessions live here"
            s = validate(env, {"path": cwd, "confidence": "medium", "reason": reason}, source="history")
            if s:
                return s
        raise _Skip("no overlap with an existing folder")

    return _attempt(env, "history", name, body)


def stage_keywords(env: Env, index: Index, name: str, deadline: float) -> Suggestion | None:
    """Stage 4: the bucket whose keywords the name mentions most, as whole words (ties: table order). Bucket only."""

    def body(info: dict) -> Suggestion:
        lower = name.lower()
        ranked = sorted(((-len(hits), i, bucket, hits) for i, (bucket, words) in enumerate(KEYWORDS.items())
                         if (hits := [w for w in words if re.search(rf"\b{re.escape(w)}\b", lower)])))
        for _n, _i, bucket, hits in ranked:
            s = validate(env, {"path": bucket, "confidence": "low", "reason": "name mentions " + ", ".join(hits)},
                         source="keywords")
            if s:
                return s
        raise _Skip("no keyword bucket" if not ranked else "keyword bucket missing on disk")

    return _attempt(env, "keywords", name, body)


def stage_root(env: Env, index: Index, name: str, deadline: float) -> Suggestion | None:
    """Stage 5: the picker opens at the tree root."""
    return _attempt(env, "root", name,
                    lambda info: Suggestion(env.root, None, "low", "no suggestion; start at the root", "root"))


# ---- the pipeline ---------------------------------------------------------------------------------------------------

class Pending:
    """The result of `suggest()`: `.local` at once; `.wait()` for the first valid model answer; `.join()` for the
    background work to finish."""

    def __init__(self, local: Suggestion):
        self.local = local
        self._answer: Suggestion | None = None
        self._lock = threading.Lock()
        self._settled = threading.Event()  # first model answer delivered, or every model stage finished
        self._thread: threading.Thread | None = None

    def wait(self, timeout: float | None = None) -> Suggestion | None:
        """The first valid model answer, or None if none came (yet) within `timeout`."""
        self._settled.wait(timeout)
        return self._answer

    def join(self, timeout: float | None = None) -> bool:
        """Wait for every model stage to end; True when they have."""
        if self._thread is not None:
            self._thread.join(timeout)
            return not self._thread.is_alive()
        return True


def _offer(env: Env, name: str, pending: Pending, s: Suggestion | None,
           on_update: Callable[[Suggestion], None]) -> None:
    if s is None:
        return
    with pending._lock:
        if pending._answer is not None:
            return  # a second model answer is discarded
        pending._answer = s
    try:
        on_update(s)
    except Exception as e:  # noqa: BLE001
        _log(env, {"ts": env.now(), "stage": "on_update", "name": name, "latency_ms": 0, "ok": False,
                   "reason_or_error": f"{type(e).__name__}: {e}", "prompt_chars": None})
    finally:
        pending._settled.set()


class _Rows:
    """A materialised `index.rows()` list, served to stages and the prompt builder (any thread)."""

    def __init__(self, rows: list):
        self._rows = rows

    def rows(self, filter_text: str = "") -> list:
        return self._rows


def _fan_out(env: Env, rows: _Rows, name: str, http: Http, runner: Runner | None, claude_exe: Path | None,
             pending: Pending, on_update: Callable[[Suggestion], None]) -> None:
    """Background thread: build the prompt from the row snapshot, then run Qwen and Claude concurrently."""
    def failed(stage: str, e: Exception) -> None:
        _log(env, {"ts": env.now(), "stage": stage, "name": name, "latency_ms": 0, "ok": False,
                   "reason_or_error": f"{type(e).__name__}: {e}", "prompt_chars": None})

    try:
        message = build_user_message(env, rows, name)
    except Exception as e:  # noqa: BLE001
        failed("prompt", e)
        pending._settled.set()
        return
    stages = [("qwen", stage_qwen(http, message=message), QWEN_TOTAL_S),
              ("claude", stage_claude(runner, exe=claude_exe, message=message), CLAUDE_TOTAL_S)]

    def work(stage: Stage, budget: float) -> None:
        try:
            _offer(env, name, pending, stage(env, rows, name, time.monotonic() + budget), on_update)
        except Exception as e:  # noqa: BLE001 - stages already catch; this is a last guard
            failed("fan_out", e)

    threads = [threading.Thread(target=work, args=(stage, budget), name=f"suggest-{label}", daemon=True)
               for label, stage, budget in stages]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    pending._settled.set()


def suggest(env: Env, index: Index, name: str, *, http: Http = default_http, runner: Runner | None = None,
            on_update: Callable[[Suggestion], None], claude_exe: Path | None = None) -> Pending:
    """Return at once with the best local answer (history, then keywords, then root) and start one daemon thread
    that builds the prompt and runs Qwen and Claude concurrently; `on_update` gets the first valid model answer,
    once, from that thread. `index.rows()` is read exactly once, here on the calling thread (its sqlite connection
    is single-threaded); everything else works from that list."""
    rows = _Rows(index.rows())
    local = (stage_history(env, rows, name, 0.0) or stage_keywords(env, rows, name, 0.0)
             or stage_root(env, rows, name, 0.0))
    pending = Pending(local)
    pending._thread = threading.Thread(
        target=_fan_out, args=(env, rows, name, http, runner, claude_exe, pending, on_update),
        name="suggest", daemon=True)
    pending._thread.start()
    return pending
