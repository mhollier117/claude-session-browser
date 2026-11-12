"""Where should a new session live? Retrieve, then choose by number.

`suggest()` ranks the existing folders against the name locally (`retrieve`) and returns the top one at once
(source "history", with `strong` saying whether it is clear enough to act on without a model; source "root" when
nothing scores). One background thread then asks the models, in order: the self-hosted Qwen endpoint first, and
Claude Code (Haiku) only when Qwen was skipped or failed. Both get the v2 prompt, a short numbered list of PROJECT
and PLACE candidates, and answer with a number.

Qwen has 6 s in total (1 s TCP connect, 2 s for the model list) and Claude 20 s, each enforced inside its own stage.
A failed connect or a model-list timeout is remembered in `data_dir/qwen-unreachable` and Qwen is skipped without a
network call for the next 10 minutes. Every stage attempt is appended to `data_dir/suggest.log` (stage, name,
latency, outcome, prompt size; never the API key or the prompt); `log_outcome` adds the folder finally chosen."""
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
from dataclasses import dataclass, field
from pathlib import Path

from . import retrieve
from .index import Index
from .paths import Env
from .scaffold import Runner, check_name
from .suggest_prompt import SYSTEM_PROMPT, Choice, build_user_message
from .tree import slug

QWEN_TOTAL_S = 6.0
QWEN_CONNECT_S = 1.0
QWEN_MODELS_S = 2.0
CLAUDE_TOTAL_S = 20.0
UNREACHABLE_FILE = "qwen-unreachable"
UNREACHABLE_TTL_S = 600.0

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
    source: str  # qwen | claude | history | root
    alternatives: list[Path] = field(default_factory=list)  # the next two ranked projects (not this one's branch)
    strong: bool = False  # local answers only: clear enough to move the picker without a model
    needs_name: bool = False  # a PLACE answer without a usable new-folder name: the UI must ask for one


class Unreachable(OSError):
    """The TCP connect to the model endpoint failed."""


@dataclass
class Prepared:
    """Retrieval for one session name: the ranked projects, the places, and the numbered prompt built from them."""
    scored: list[retrieve.Scored]
    places: list[retrieve.Place]
    message: str
    choices: list[Choice]


def prepare(env: Env, rows: list, name: str) -> Prepared:
    """Rank the folders for `name` (from the row list) and build the v2 user message."""
    cands = retrieve.candidates(env, None, rows)
    scored = retrieve.rank(name, cands, env.now())
    places = retrieve.places(env, cands, name, rows)
    message, choices = build_user_message(name, scored, places)
    return Prepared(scored, places, message, choices)


def _same_branch(a: Path, b: Path) -> bool:
    pa, pb = [p.lower() for p in Path(a).parts], [p.lower() for p in Path(b).parts]
    n = min(len(pa), len(pb))
    return pa[:n] == pb[:n]


def _alternatives(prep: Prepared, chosen: Path, n: int = 2) -> list[Path]:
    """The next `n` ranked projects that are neither `chosen` nor its ancestor or descendant."""
    return [s.cand.path for s in prep.scored if not _same_branch(s.cand.path, chosen)][:n]


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
            check_name(new_folder)
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


def _place_answer(folder: Path, value) -> tuple[Path, str | None, bool]:
    """Where a PLACE answer leads: (folder, new folder name, needs a name). A usable new name is slugged and
    checked; a name that already exists as a child folder leads into that folder (the existing project, no new
    folder); null, empty or unusable names give the place itself with no name."""
    if not isinstance(value, str) or value.strip().lower() in _NULLS:
        return folder, None, False
    name = slug(value.strip())
    try:
        check_name(name)
    except ValueError:
        return folder, None, True
    try:
        existing = next((c for c in folder.iterdir() if c.is_dir() and c.name.lower() == name.lower()), None)
    except OSError:
        existing = None
    if existing is not None:
        return existing, None, False
    return folder, name, False


def validate_choice(env: Env, raw, prep: Prepared, source: str = "") -> Suggestion | None:
    """A v2 answer (`{"choice": n, "new_folder", "confidence", "reason"}`) as a Suggestion, or None unless `choice`
    is an int naming one of the numbered candidates. A PROJECT never carries a new folder. For a PLACE:
    - a usable new name is proposed as given (slugged);
    - a name that already exists as a child folder answers with that folder itself (no new folder);
    - null means "start in the place itself", but only for a place that already has sessions of its own;
      otherwise (and for an unusable name) the answer is the place with `needs_name` set, so the UI asks."""
    if not isinstance(raw, dict):
        return None
    n = raw.get("choice")
    if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= len(prep.choices):
        return None
    choice = prep.choices[n - 1]
    path, new_folder, needs_name = choice.path, None, False
    if choice.kind == "PLACE":
        path, new_folder, needs_name = _place_answer(choice.path, raw.get("new_folder"))
        if path == choice.path and new_folder is None and not needs_name:
            place = next((p for p in prep.places if p.path == choice.path), None)
            needs_name = place is None or place.sessions_here == 0
    confidence = raw.get("confidence") if raw.get("confidence") in _CONFIDENCE else "low"
    reason = " ".join(raw["reason"].split()) if isinstance(raw.get("reason"), str) else ""
    return Suggestion(path, new_folder, confidence, reason, source, alternatives=_alternatives(prep, path),
                      needs_name=needs_name)

def _model_answer(env: Env, text, prep: Prepared, source: str) -> Suggestion:
    raw = _parse_json(text)
    if raw is None:
        raise _Skip("answer is not JSON")
    s = validate_choice(env, raw, prep, source=source)
    if s is None:
        raise _Skip(f"answer failed validation: choice={raw.get('choice')!r} of {len(prep.choices)}")
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
    on the LAN/VPN) and without following redirects. Returns (status, parsed JSON or text); a failed connect check
    raises `Unreachable`, other network errors raise as they come."""
    parts = urllib.parse.urlsplit(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        with socket.create_connection((parts.hostname, port), timeout=min(QWEN_CONNECT_S, timeout)):
            pass
    except OSError as e:
        raise Unreachable(f"connect to {parts.hostname}:{port} failed: {type(e).__name__}") from None
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


def qwen_request(model: str, message: str) -> dict:
    """The chat-completions body the Qwen stage sends (also used by tools/suggest_eval.py)."""
    return {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": message}],
        "chat_template_kwargs": {"enable_thinking": False},
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "max_tokens": 400,
    }


def _is_timeout(e: BaseException) -> bool:
    return isinstance(e, TimeoutError) or (isinstance(e, urllib.error.URLError)
                                           and isinstance(e.reason, TimeoutError))


def _unreachable_cached(env: Env) -> bool:
    try:
        when = float((env.data_dir / UNREACHABLE_FILE).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return 0 <= env.now() - when < UNREACHABLE_TTL_S


def _remember_unreachable(env: Env) -> None:
    try:
        env.data_dir.mkdir(parents=True, exist_ok=True)
        (env.data_dir / UNREACHABLE_FILE).write_text(repr(env.now()), encoding="utf-8")
    except OSError:
        pass  # only an optimisation: the next attempt pays the connect timeout again


def _prepared(env: Env, index, name: str, prepared: Prepared | None) -> Prepared:
    return prepared if prepared is not None else prepare(env, index.rows(), name)


def stage_qwen(http: Http = default_http, prepared: Prepared | None = None) -> Stage:
    """Stage 1: the self-hosted Qwen endpoint. `prepared` is the retrieval and prompt for this name (worked out
    from `index.rows()` when None). Skipped without a network call while `data_dir/qwen-unreachable` is fresh."""

    def run(env: Env, index: Index, name: str, deadline: float) -> Suggestion | None:
        def body(info: dict) -> Suggestion:
            end = min(deadline, time.monotonic() + QWEN_TOTAL_S)
            key, base, configured = _qwen_config(env)
            info["secret"] = key
            if _unreachable_cached(env):
                raise _Skip("skipped: unreachable (cached)")
            prep = _prepared(env, index, name, prepared)
            info["prompt_chars"] = len(SYSTEM_PROMPT) + len(prep.message)
            headers = {"Authorization": f"Bearer {key}"}
            try:
                status, data = http("GET", base + "/models", headers, None, min(QWEN_MODELS_S, _remaining(end)))
                alias = _current_alias(data) if status == 200 else None
            except _Skip:
                raise
            except Exception as e:  # noqa: BLE001 - no model list: fall back to the configured name
                if isinstance(e, Unreachable) or _is_timeout(e):
                    _remember_unreachable(env)
                    raise _Skip(f"unreachable: {type(e).__name__}: {e}") from None
                alias = None
            try:
                status, data = http("POST", base + "/chat/completions", headers,
                                    qwen_request(alias or configured, prep.message), _remaining(end))
            except Unreachable as e:
                _remember_unreachable(env)
                raise _Skip(f"unreachable: {e}") from None
            _remaining(end)  # an answer that arrives after the deadline is dropped
            if status != 200:
                raise _Skip(f"HTTP {status}")
            try:
                content = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                raise _Skip("no choices[0].message.content in the response") from None
            info["answer"] = content
            return _model_answer(env, content, prep, "qwen")

        return _attempt(env, "qwen", name, body)

    return run


def _find_claude() -> Path | None:
    found = shutil.which("claude.exe")
    if found:
        return Path(found)
    fallback = Path.home() / ".local" / "bin" / "claude.exe"
    return fallback if fallback.is_file() else None


def stage_claude(runner: Runner | None = None, exe: Path | None = None, prepared: Prepared | None = None) -> Stage:
    """Stage 2: Haiku through the signed-in Claude Code CLI (`claude.exe -p`), run from `data_dir/suggest-cwd`.
    `exe` overrides the PATH / ~/.local/bin lookup; `prepared` as in `stage_qwen`."""
    runner = runner or Runner()

    def run(env: Env, index: Index, name: str, deadline: float) -> Suggestion | None:
        def body(info: dict) -> Suggestion:
            end = min(deadline, time.monotonic() + CLAUDE_TOTAL_S)
            binary = Path(exe) if exe is not None else _find_claude()
            if binary is None or not binary.is_file():
                raise _Skip("claude.exe not found")
            prep = _prepared(env, index, name, prepared)
            info["prompt_chars"] = len(SYSTEM_PROMPT) + len(prep.message)
            cwd = env.data_dir / "suggest-cwd"
            cwd.mkdir(parents=True, exist_ok=True)
            # the user message goes on stdin (print mode reads the prompt from it): no command-line length limit
            args = [str(binary), "-p", "--model", "haiku", "--output-format", "json",
                    "--system-prompt", SYSTEM_PROMPT, "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                    "--tools", "", "--no-session-persistence"]
            env_ = {k: v for k, v in os.environ.items() if k.upper() not in _CLAUDE_ENV_DROP}
            res = runner.run(args, cwd, env=env_, timeout=_remaining(end), input=prep.message)
            if res.code != 0:
                raise _Skip(f"claude exited {res.code}: {res.err.strip()[:200]}")
            try:
                out = json.loads(res.out)
            except ValueError:
                raise _Skip("claude output is not JSON") from None
            if not isinstance(out, dict) or out.get("is_error"):
                raise _Skip("claude reported is_error")
            info["answer"] = out.get("result")
            return _model_answer(env, out.get("result"), prep, "claude")

        return _attempt(env, "claude", name, body)

    return run


# ---- local stages ---------------------------------------------------------------------------------------------------

def stage_history(env: Env, index: Index, name: str, deadline: float,
                  prepared: Prepared | None = None) -> Suggestion | None:
    """Stage 3 (local): the top-ranked existing folder (see `retrieve.rank`), with `strong` from `retrieve.strong`
    and the next two ranked folders as alternatives."""

    def body(info: dict) -> Suggestion:
        prep = _prepared(env, index, name, prepared)
        if not prep.scored:
            raise _Skip("no folder matches a word of the name")
        top = prep.scored[0]
        sure = retrieve.strong(prep.scored)
        return Suggestion(top.cand.path, None, "high" if sure else "medium", "matches " + ", ".join(top.matched),
                          "history", alternatives=_alternatives(prep, top.cand.path), strong=sure)

    return _attempt(env, "history", name, body)


def stage_root(env: Env, index: Index, name: str, deadline: float) -> Suggestion | None:
    """Stage 4 (local): the picker opens at the tree root."""
    return _attempt(env, "root", name,
                    lambda info: Suggestion(env.root, None, "low", "no suggestion; start at the root", "root"))


# ---- outcome --------------------------------------------------------------------------------------------------------

def _rel_or_none(env: Env, path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        rel = Path(path).relative_to(env.root)
    except ValueError:
        return str(path)
    return None if str(rel) == "." else str(rel)


def log_outcome(env: Env, name: str, suggestion: Suggestion | None, chosen: Path, new_folder: str | None) -> None:
    """One `suggest.log` line recording where the user finally started: `chosen` (the folder, or the parent of
    `new_folder`) against the suggestion in effect. `match` is true when both name the same final folder (same
    folder, same new folder name or none), compared case-insensitively. A root suggestion counts as none."""
    suggested = None if suggestion is None else _rel_or_none(env, suggestion.path)

    def final(folder: Path, sub: str | None) -> str:
        return str(Path(folder) / sub if sub else Path(folder)).lower()

    match = suggested is not None and final(suggestion.path, suggestion.new_folder) == final(chosen, new_folder)
    _log(env, {"ts": env.now(), "stage": "outcome", "name": name, "suggested": suggested,
               "suggested_source": suggestion.source if suggestion is not None else None,
               "chosen": _rel_or_none(env, chosen) or "", "new_folder": new_folder, "match": match})


# ---- the pipeline ---------------------------------------------------------------------------------------------------

class Pending:
    """The result of `suggest()`: `.local` at once; `.wait()` for the first valid model answer; `.join()` for the
    background work to finish. With `defer_models` the model stages start only once `release_models()` is called
    (directly or by `release_after`); `cancel()` drops the answer and keeps stages that have not started from
    starting."""

    def __init__(self, local: Suggestion, defer_models: bool = False):
        self.local = local
        self._answer: Suggestion | None = None
        self._lock = threading.Lock()
        self._settled = threading.Event()  # first model answer delivered, every model stage finished, or cancelled
        self._release = threading.Event()  # the coordinator may start the model stages
        self._cancelled = False
        self._timer: threading.Timer | None = None
        self._thread: threading.Thread | None = None
        if not defer_models:
            self._release.set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def release_models(self) -> None:
        """Let the coordinator start the model stages (no effect once cancelled or already released)."""
        self._release.set()

    def release_after(self, delay: float) -> None:
        """Release the model stages in `delay` seconds unless `cancel()` comes first."""
        with self._lock:
            if self._cancelled:
                return
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(delay, self.release_models)
            self._timer.daemon = True
            self._timer.start()

    def cancel(self) -> None:
        """Discard any model answer (no `on_update`; `wait()` returns None) and do not start the model stages if
        they have not started yet. Stages already running finish in the background; their answer is dropped."""
        with self._lock:
            self._cancelled = True
            self._answer = None
            timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()
        self._release.set()  # wake a waiting coordinator, which sees the flag and stops
        self._settled.set()

    def wait(self, timeout: float | None = None) -> Suggestion | None:
        """The first valid model answer, or None if none came (yet) within `timeout`, or after `cancel()`."""
        self._settled.wait(timeout)
        with self._lock:
            return None if self._cancelled else self._answer

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
        if pending._answer is not None or pending._cancelled:
            return  # a second model answer, or any answer after cancel(), is discarded
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


def _run_models(env: Env, rows: _Rows, name: str, prep: Prepared | None, http: Http, runner: Runner | None,
                claude_exe: Path | None, pending: Pending, on_update: Callable[[Suggestion], None]) -> None:
    """Background thread: wait until the model stages are released (at once unless deferred), then run Qwen and,
    only when Qwen gave no valid answer, Claude, one after the other. A cancelled request starts nothing more."""
    try:
        pending._release.wait()
        if pending.cancelled or prep is None:
            return
        stages = [(stage_qwen(http, prepared=prep), QWEN_TOTAL_S),
                  (stage_claude(runner, exe=claude_exe, prepared=prep), CLAUDE_TOTAL_S)]
        for stage, budget in stages:
            if pending.cancelled:
                return
            try:
                answer = stage(env, rows, name, time.monotonic() + budget)
            except Exception as e:  # noqa: BLE001 - stages already catch; this is a last guard
                _log(env, {"ts": env.now(), "stage": "models", "name": name, "latency_ms": 0, "ok": False,
                           "reason_or_error": f"{type(e).__name__}: {e}", "prompt_chars": None})
                answer = None
            if answer is not None:
                _offer(env, name, pending, answer, on_update)
                return
    finally:
        pending._settled.set()


def suggest(env: Env, index: Index, name: str, *, http: Http = default_http, runner: Runner | None = None,
            on_update: Callable[[Suggestion], None], claude_exe: Path | None = None,
            defer_models: bool = False) -> Pending:
    """Return at once with the local answer (the top-ranked folder, else the root) and start one daemon thread that
    asks Qwen, then Claude only if Qwen gave no valid answer; `on_update` gets the model answer, once, from that
    thread. `index.rows()` is read exactly once, here on the calling thread (its sqlite connection is
    single-threaded), and the retrieval and prompt are worked out here too. `defer_models`: the thread waits for
    `Pending.release_models()` (or `cancel()`) before starting the model stages."""
    try:
        rows = _Rows(index.rows())
    except Exception as e:  # noqa: BLE001 - suggest() never raises: carry on with no history
        _log(env, {"ts": env.now(), "stage": "index", "name": name, "latency_ms": 0, "ok": False,
                   "reason_or_error": f"{type(e).__name__}: {e}", "prompt_chars": None})
        rows = _Rows([])
    try:
        prep = prepare(env, rows.rows(), name)
    except Exception as e:  # noqa: BLE001
        _log(env, {"ts": env.now(), "stage": "prompt", "name": name, "latency_ms": 0, "ok": False,
                   "reason_or_error": f"{type(e).__name__}: {e}", "prompt_chars": None})
        prep = None
    local = ((stage_history(env, rows, name, 0.0, prepared=prep) if prep is not None else None)
             or stage_root(env, rows, name, 0.0))
    pending = Pending(local, defer_models=defer_models)
    pending._thread = threading.Thread(
        target=_run_models, args=(env, rows, name, prep, http, runner, claude_exe, pending, on_update),
        name="suggest", daemon=True)
    pending._thread.start()
    return pending
