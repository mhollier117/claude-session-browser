# Session Launcher (`cc`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `launcher/` package inside the Claude Session Browser fork that indexes Claude Code and Qwen Code sessions, resumes them, and creates new, correctly placed and named sessions, exposed as a terminal command `cc` and inside the browser.

**Architecture:** Pure-Python core (`launcher/`) with no UI imports: stores → sqlite index → tree model → suggestion pipeline → scaffold → launch commands. Two thin surfaces: a prompt_toolkit terminal app (`launcher/tui/`) driven by a PowerShell function `cc`, and new `Api` methods plus HTML in the existing browser monolith. Every filesystem, subprocess and network call goes through an injectable seam so tests run against a fake home under `tmp_path`, never the real `~\.claude`.

**Tech Stack:** Python 3.11+ (fork `.venv`; Python 3.14 on the box), pytest, prompt_toolkit (new runtime dep), sqlite3, urllib (Qwen endpoint), subprocess (`claude.exe`, `qwen`, `git`, `gh`, `wt`), existing `sessions_live.py` and `termfocus.py`, pywebview `Api` bridge.

**Spec:** `docs/superpowers/specs/2026-11-05-session-launcher-design.md` (moved into the fork by Task 1; until then `C:\claude-code\cc\launcher\docs\superpowers\specs\2026-11-05-session-launcher-design.md`).

## Global Constraints

- Repo: `mhollier117/claude-session-browser`, working copy `C:\claude-code\claude_improvements\claude-session-browser-main`. Work on branch `launcher` in a **git worktree** (the main working copy has uncommitted edits in `accounts.py` and `tests/test_accounts.py` that must not be touched or committed).
- The launcher never writes into `~\.claude` or `~\.qwen` except its own `%LOCALAPPDATA%\cc-launcher\` (index, logs, `suggest-cwd`). Never a transcript, `.claude.json` or `history.jsonl`.
- Tests never read the real home: every test builds its store under `tmp_path` and passes it in. CI command stays `python -m pytest -q tests` (build.ps1:51).
- Tree root `C:\claude-code`, areas `work`, `home`, `cc`; buckets under `work`: `clients, wf, rewst, halo, ninja, cipp, m365, net, vendors, scripts`; under `home`: `homelab, ai` or a project directly. Organisational levels: `work` L1, `work\<bucket>`, `work\clients\<client>`.
- Folder slug rule: lower-case, runs of characters outside `[A-Za-z0-9._-]` → `_`, trim `_`.
- Launch name flag `-n "<name>"` (Claude). Resume: Claude `<wrapper> -r <sid>`, Qwen `qwen -r <sid>`.
- Qwen model id: the `*-current` alias from `GET /v1/models`, else `settings.model.name`; request `chat_template_kwargs: {"enable_thinking": false}`, `temperature: 0`, `response_format: {"type": "json_object"}`, `max_tokens: 400`; 1 s connect, 6 s total.
- Claude suggestion call: `claude.exe -p <user msg> --model haiku --output-format json --system-prompt <system> --strict-mcp-config --mcp-config {"mcpServers":{}} --tools "" --no-session-persistence`, cwd `%LOCALAPPDATA%\cc-launcher\suggest-cwd`, 20 s cap, concurrent with Qwen.
- Suggestion is never applied without user confirmation; a model answer is discarded unless `path` exists under the root and `new_folder` (if any) passes the slug rule.
- GitHub: personal (`mhollier117`, plain `gh`) by default; work follows `~\.claude\templates\work-github\WORK-GITHUB.md`; never `gh auth switch`; never write a token anywhere.
- No `.claude-project` placeholder: write it only after a successful push, with `repo`, `lastCommit`, `lastSynced`.
- Commits: conventional prefix (`feat:`, `test:`, `chore:`), end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. Subagents never push.

## Review Focus

1. A transcript whose last user record is a `tool_result` (not a typed ask): the preview must show the last **typed** ask, not tool output. Test in Task 2.
2. A session folder whose `cwd` no longer exists on disk (moved by the reorg): the row must still list, with the recorded path, and resume must warn rather than fail silently. Test in Task 3 and Task 7.
3. Two sessions with the same display name in different folders: both rows shown, both resumable, filter matches both. Test in Task 3.
4. Session name containing quotes or `$` (`fix "auth" $env thing`): the launch command must pass it to `-n` intact without shell injection. Test in Task 7.
5. The Qwen endpoint answering with a path that does not exist, or non-JSON, or after the deadline: the picker must still open on the local answer and never hang. Test in Task 6.

---

### Task 1: Branch, worktree, package skeleton, fake-home fixture

**Files:**
- Create: `launcher/__init__.py`, `launcher/paths.py`, `tests/launcher/__init__.py`, `tests/launcher/conftest.py`, `docs/superpowers/specs/2026-11-05-session-launcher-design.md` (moved), `docs/superpowers/plans/2026-11-05-session-launcher.md` (moved)
- Modify: `requirements.txt` (add `prompt_toolkit>=3.0.48`), `.gitignore` (add `.superpowers/`)

**Interfaces:**
- Produces: `launcher.paths.Env` dataclass: `root: Path` (tree root), `claude_home: Path`, `qwen_home: Path`, `data_dir: Path` (`%LOCALAPPDATA%\cc-launcher`), `now: Callable[[], float]`; `launcher.paths.default_env() -> Env` reading `CC_LAUNCHER_ROOT`, `CLAUDE_CONFIG_DIR`, `CC_LAUNCHER_QWEN_HOME`, `CC_LAUNCHER_DATA` env overrides.
- Produces: conftest fixture `fake_env(tmp_path) -> Env` with `root/work/{clients/bast/bifrost,net,halo,vendors}`, `root/home/{homelab,ai}`, `root/cc`, empty `claude_home/projects`, `claude_home/sessions`, `qwen_home/projects`, `qwen_home/sessions`; helper `write_claude_session(env, cwd: Path, sid: str, records: list[dict]) -> Path` (encodes the folder name the Claude way: every char not `[A-Za-z0-9]` → `-`) and `write_qwen_session(env, work_dir: Path, sid: str, records: list[dict]) -> Path` (folder = sha-free lower-cased encoding, `chats/<sid>.jsonl` + `<sid>.runtime.json` with `{"work_dir": str(work_dir)}`; copy the exact layout from `C:\claude-code\cc\reorg\reorg\stores.py`).

- [ ] **Step 1: Worktree and branch**

```powershell
Set-Location C:\claude-code\claude_improvements\claude-session-browser-main
git worktree add ..\csb-launcher -b launcher main
Set-Location ..\csb-launcher
```
All later tasks run in `C:\claude-code\claude_improvements\csb-launcher`.

- [ ] **Step 2: Move the spec and this plan into `docs/superpowers/` of the worktree; add `.superpowers/` to `.gitignore`; add `prompt_toolkit>=3.0.48` to `requirements.txt`; `pip install -r requirements-dev.txt` in the fork `.venv`.**

- [ ] **Step 3: Write the failing test** `tests/launcher/test_paths.py`:

```python
def test_default_env_honours_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("CC_LAUNCHER_ROOT", str(tmp_path / "r"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "c"))
    monkeypatch.setenv("CC_LAUNCHER_QWEN_HOME", str(tmp_path / "q"))
    monkeypatch.setenv("CC_LAUNCHER_DATA", str(tmp_path / "d"))
    env = default_env()
    assert (env.root, env.claude_home, env.qwen_home, env.data_dir) == (tmp_path/"r", tmp_path/"c", tmp_path/"q", tmp_path/"d")

def test_fake_env_layout(fake_env):
    assert (fake_env.root / "work" / "clients" / "bast" / "bifrost").is_dir()
    assert (fake_env.claude_home / "projects").is_dir() and (fake_env.qwen_home / "projects").is_dir()
```

- [ ] **Step 4: Run** `python -m pytest tests/launcher -q` → FAIL (module not found).
- [ ] **Step 5: Implement `launcher/paths.py` and `tests/launcher/conftest.py`** per Interfaces.
- [ ] **Step 6: Run** `python -m pytest tests/launcher -q` → PASS; `python -m pytest -q tests` still passes (existing suite).
- [ ] **Step 7: Commit** `chore(launcher): package skeleton, fake-home fixture, spec and plan`.

---

### Task 2: Transcript readers (Claude and Qwen)

**Files:**
- Create: `launcher/stores.py`, `tests/launcher/test_stores.py`

**Interfaces:**
- Produces: `@dataclass SessionFacts: store: str ("claude"|"qwen"), sid: str, path: Path, cwd: str|None, name: str|None, first_ask: str, last_ask: str, last_reply: str, user_msgs: int, assistant_msgs: int, size: int, mtime: float`.
- Produces: `read_claude(path: Path, tail_bytes: int = 262_144) -> SessionFacts`; `read_qwen(path: Path, tail_bytes: int = 262_144) -> SessionFacts`; `is_top_level_claude(path: Path) -> bool` (false for `agent-*.jsonl`, files under `<sid>/subagents/`, `.bak`, `.companion.json`); `full_last_reply(path: Path, store: str) -> str` (whole last assistant message, read on demand for the preview); `clean_user_text(s: str) -> str` (port of `claude_sessions.clean_user_text`: strip `<system-reminder>…</system-reminder>`, `<command-*>` wrappers, `<local-command-stdout>`; collapse whitespace).
- Record rules (Claude): top-level `type` in `user|assistant|ai-title|custom-title`; `cwd` first seen wins; user text = `message.content` when str, else join of blocks with `type=="text"`; a user record whose content is only `tool_result` blocks is **not** an ask; assistant text = join of text blocks. `name` = last `custom-title` → last `ai-title` → None (registry names are added in Task 3). Head read: first 64 KB then grow by doubling until a first ask is found or EOF. Tail read: last `tail_bytes`, parse from the first complete line, grow backwards by doubling until both a last ask and a last reply are found or BOF.
- Qwen: implementer reads `C:\claude-code\cc\reorg\reorg\stores.py` for the chat line shape and `.runtime.json` (`work_dir`); map to the same facts.

- [ ] **Step 1: Write the failing tests**

```python
def test_claude_first_and_last_ask_skip_tool_results(fake_env):
    p = write_claude_session(fake_env, fake_env.root/"work"/"net", "s1", [
        {"type":"user","cwd":str(fake_env.root/"work"/"net"),"timestamp":"2026-11-01T10:00:00Z","message":{"role":"user","content":"set up PXE <system-reminder>x</system-reminder>"}},
        {"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"Reply one."}]}},
        {"type":"user","message":{"role":"user","content":"why no DHCP offer?"}},
        {"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"Add the VLAN."}]}},
        {"type":"user","message":{"role":"user","content":[{"type":"tool_result","content":"ok"}]}},
    ])
    f = read_claude(p)
    assert (f.first_ask, f.last_ask, f.last_reply) == ("set up PXE", "why no DHCP offer?", "Add the VLAN.")
    assert f.cwd == str(fake_env.root/"work"/"net") and f.user_msgs == 2 and f.assistant_msgs == 2

def test_claude_name_custom_title_beats_ai_title(fake_env): ...  # custom-title last wins
def test_tail_read_grows_when_last_reply_is_large(fake_env): ...  # 600 KB assistant text, tail_bytes=4096
def test_is_top_level_claude_filters_agent_and_subagent_files(fake_env): ...
def test_read_qwen_uses_runtime_work_dir(fake_env): ...
```

- [ ] **Step 2: Run** `python -m pytest tests/launcher/test_stores.py -q` → FAIL.
- [ ] **Step 3: Implement `launcher/stores.py`** per Interfaces.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(launcher): Claude and Qwen transcript readers`.

---

### Task 3: Index (sqlite) with names, live pids and refresh rules

**Files:**
- Create: `launcher/index.py`, `tests/launcher/test_index.py`

**Interfaces:**
- Consumes: Task 2 readers; `sessions_live.LiveSessions(claude_home).snapshot()` (list of dicts with `session_id, pid, name, named, cwd`); `~\.claude\session_titles.json` (`{sid: title}`); `~\.claude\sessions\<pid>.json` and `~\.qwen\sessions\<pid>.json` registries (shape: see `C:\claude-code\cc\reorg\reorg\liveness.py`).
- Produces: `class Index(env: Env, db_path: Path|None = None)` with `refresh(progress: Callable[[int,int],None]|None = None) -> int` (rows touched), `rows(filter_text: str = "") -> list[Row]` sorted by `last_activity` desc, live first within equal minute; `row(store, sid) -> Row|None`; `record_launch(store, sid, wrapper: str)`; `folder_stats(rel_path: str) -> FolderStats(sessions: int, last_activity: float|None, names: list[str])` for Task 4.
- `@dataclass Row: store, sid, cwd, name, display_name, first_ask, last_ask, last_reply (first 400 chars), user_msgs, assistant_msgs, size, mtime, last_activity, live_pid: int|None, wrapper: str|None, cwd_exists: bool`.
- `display_name` priority: `session_titles.json` entry → transcript `custom-title` → live registry name when `named` → transcript `ai-title` → registry name → first 60 chars of `first_ask` → `"(untitled)"`.
- Refresh rule: list files every call; re-read only when `(size, mtime)` changed; delete rows whose file vanished; skip any file under `data_dir/suggest-cwd`'s encoded project folder.
- Schema: table `sessions(store, sid, path, cwd, name, display_name, first_ask, last_ask, last_reply, user_msgs, assistant_msgs, size, mtime, last_activity, wrapper, PRIMARY KEY(store, sid))`; `live` is computed at `rows()` time, not stored.

- [ ] **Step 1: Write the failing tests**

```python
def test_refresh_indexes_both_stores_and_sorts_by_activity(fake_env): ...
def test_refresh_rereads_only_changed_files(fake_env, monkeypatch): ...  # count read_claude calls
def test_display_name_priority(fake_env): ...  # titles.json > custom-title > ai-title > first ask
def test_rows_filter_matches_name_path_and_first_ask(fake_env): ...
def test_two_sessions_same_name_both_listed(fake_env): ...
def test_cwd_missing_flags_row(fake_env): ...  # cwd_exists False, row still present
def test_live_pid_from_registry(fake_env, monkeypatch): ...  # fake LiveSessions snapshot
def test_suggest_cwd_sessions_excluded(fake_env): ...
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement `launcher/index.py`.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(launcher): sqlite session index with names and liveness`.

---

### Task 4: Tree model, level kinds, fragment jump, path display

**Files:**
- Create: `launcher/tree.py`, `launcher/display.py`, `tests/launcher/test_tree.py`, `tests/launcher/test_display.py`

**Interfaces:**
- Consumes: `Index.folder_stats`.
- Produces (`tree.py`): `Kind = Literal["area","bucket","client","project","container","root"]`; `level_kind(root: Path, folder: Path) -> Kind` (root → `root`; `work|home|cc` → `area`; `work\<bucket>` → `bucket`; `work\clients\<x>` → `client`; `home\<x>`/`cc\<x>` → `project`; else `project`); `new_folder_kind_options(root, parent) -> list[str]` (`["organisational"]` for `work` L1/L2 and `work\clients`; `["client"]` under `work\clients`; `["project","container"]` elsewhere, default first); `children(env, index, folder) -> list[Child(name, path, kind, stats)]` (dirs only, skip names starting with `.` or `_`, skip `node_modules`); `resolve_fragment(env, folder, fragment: str) -> list[Path]` (each `/`-separated piece prefix-matches, case-insensitive, one level; returns all full matches, e.g. `cl/ba/bi` → `[…\bifrost, …\bifrost_old]`); `slug(name: str) -> str`.
- Produces (`display.py`): `rel_or_abs(root, path) -> str`; `middle_ellipsis(path_str: str, width: int) -> str` (keeps first segment and last two segments; drops middle segments replaced by `…`; if still too wide, truncates the first segment, never the last two); `age(ts: float, now: float) -> str` (`"2h"`, `"3d"`, month abbreviation beyond 30 days, `"—"` for None).

- [ ] **Step 1: Write the failing tests**

```python
def test_level_kinds(fake_env):
    r = fake_env.root
    assert level_kind(r, r/"work") == "area" and level_kind(r, r/"work"/"net") == "bucket"
    assert level_kind(r, r/"work"/"clients"/"bast") == "client" and level_kind(r, r/"cc"/"reorg") == "project"
def test_new_folder_kind_options(fake_env): ...
def test_resolve_fragment_cl_ba_bi(fake_env): ...  # bifrost and bifrost_old both returned
def test_slug(): assert slug("Stackjack rip, phase 2!") == "stackjack_rip_phase_2"
def test_middle_ellipsis_keeps_last_two_segments():
    assert middle_ellipsis(r"work\wf\Completion_Handler_-JBF-_New_user_2026-08-25", 40).endswith(r"\Completion_Handler_-JBF-_New_user_2026-08-25"[-30:])
def test_age(): ...
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(launcher): tree model, fragment jump, path display`.

---

### Task 5: Scaffold (folders, plan.md, git, GitHub, marker)

**Files:**
- Create: `launcher/scaffold.py`, `launcher/templates/plan.md`, `launcher/templates/CLAUDE.client.md`, `tests/launcher/test_scaffold.py`

**Interfaces:**
- Consumes: Task 4 `slug`, `new_folder_kind_options`.
- Produces: `@dataclass Runner: run(args: list[str], cwd: Path, env: dict|None=None, timeout: float=60) -> Result(code, out, err)`; `create_folder(parent: Path, name: str, kind: str, session_name: str, runner: Runner, github: str|None) -> Created(path: Path, repo: str|None, warnings: list[str])`.
- Behaviour per kind: `organisational` → mkdir only. `client` → mkdir + `CLAUDE.md` from `templates/CLAUDE.client.md` (`# {name}` + `Client work for {name}.`). `container` → mkdir only. `project` → mkdir, `plan.md` from `templates/plan.md` with `{title}` = session name, `git init`, `git add -A`, `git commit -m "chore: scaffold"`, then if `github == "personal"`: `gh repo create <slug> --private --source . --push`; if `"work"`: the sequence in `~\.claude\templates\work-github\WORK-GITHUB.md` (implementer reads it; token obtained per command via `gh auth token --user mhollierBPT` into the env of that one `gh` call, never written); on success write `.claude-project` `{repo, lastCommit (git rev-parse HEAD), lastSynced (UTC ISO)}`; on any `gh` failure append a warning and leave `.git` only.
- `templates/plan.md` content (exact): `# {title}\n\n## Goal\n\n{title}\n\n## Current phase\n\nNot started.\n\n## Next\n\n- \n\n## Decisions\n\n## Known issues\n\nNone.\n`

- [ ] **Step 1: Write the failing tests** with a `FakeRunner` recording calls and returning scripted results:

```python
def test_project_scaffold_personal(fake_env):
    runner = FakeRunner({"gh repo create": (0, "", ""), "git rev-parse HEAD": (0, "abc123\n", "")})
    c = create_folder(fake_env.root/"work"/"net", "fortinet_api_v2", "project", "FortiOS 7.6 auth", runner, "personal")
    assert (c.path/"plan.md").read_text().startswith("# FortiOS 7.6 auth")
    assert ["gh","repo","create","fortinet_api_v2","--private","--source",".","--push"] in runner.calls
    assert json.loads((c.path/".claude-project").read_text())["lastCommit"] == "abc123"
def test_project_scaffold_gh_failure_leaves_git_only(fake_env): ...  # no .claude-project, one warning
def test_client_folder_gets_claude_md(fake_env): ...
def test_organisational_and_container_make_folder_only(fake_env): ...
def test_work_account_never_writes_token(fake_env): ...  # token only in env of the gh call; no file contains it
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(launcher): project, client and container scaffold with GitHub creation`.

---

### Task 6: Suggestion pipeline

**Files:**
- Create: `launcher/suggest.py`, `launcher/suggest_prompt.py`, `tests/launcher/test_suggest.py`, `tests/launcher/test_suggest_prompt.py`

**Interfaces:**
- Consumes: `Index.rows`, `Index.folder_stats`, Task 4 `children`, `slug`.
- Produces (`suggest_prompt.py`): `SYSTEM_PROMPT: str` (verbatim from spec §Prompt, "Decide in this order" version); `build_user_message(env, index, session_name: str, max_lines: int = 400) -> str` (tree listing depth 4 under `work`/`home`, depth 2 under `cc`, **including empty buckets** as `path · 0 · —`, each line `rel\path · <sessions> · <age> · "name1", "name2", "name3"`; then "Most recent 15 sessions overall"; drop oldest empty folders first when over `max_lines`).
- Produces (`suggest.py`): `@dataclass Suggestion: path: Path, new_folder: str|None, confidence: str, reason: str, source: str ("qwen"|"claude"|"history"|"keywords"|"root")`; `validate(env, raw: dict) -> Suggestion|None`; stages as callables `(env, index, name, deadline) -> Suggestion|None`: `stage_qwen(http: Callable)`, `stage_claude(runner)`, `stage_history`, `stage_keywords`, `stage_root`; `KEYWORDS: dict[str, tuple[str,...]]` (copy from `cc-new-spike.ps1`); `suggest(env, index, name, *, http, runner, on_update: Callable[[Suggestion], None]) -> Suggestion` returns the best **local** answer immediately (history → keywords → root) and starts a daemon thread running Qwen and Claude concurrently; the first valid model answer is passed to `on_update` once; later answers are dropped; every stage logs prompt/answer/latency to `data_dir/suggest.log`.
- Qwen stage: read `qwen_home/settings.json` → `model.baseUrl`, provider entry `envKey` → `env[envKey]`; `GET /v1/models` (2 s) → choose id ending `-current`, else `model.name`; TCP connect 1 s, total 6 s; POST per Global Constraints; parse `choices[0].message.content` as JSON, falling back to the first `{…}` block.
- Claude stage: command per Global Constraints via `runner`, cwd `data_dir/suggest-cwd` (created), parse stdout JSON `result` field then the inner JSON; skip if `is_error`.

- [ ] **Step 1: Write the failing tests**

```python
def test_user_message_includes_empty_buckets_and_recent_names(fake_env): ...
def test_validate_rejects_missing_path_and_bad_slug(fake_env):
    assert validate(fake_env, {"path": r"work\vendors\keeper", "new_folder": None, "confidence":"high","reason":"x"}) is None
    assert validate(fake_env, {"path": r"work\vendors", "new_folder": "Keeper SSO", ...}) is None
def test_qwen_stage_picks_current_alias_and_thinking_off(fake_env): ...  # fake http records request body
def test_qwen_stage_times_out_silently(fake_env): ...  # fake http sleeps past deadline → None
def test_claude_stage_parses_result_json(fake_env): ...  # FakeRunner returns {"is_error":false,"result":"{...}"}
def test_history_stage_matches_word_overlap(fake_env): ...  # "stackjack" → bifrost\stackjack_rip_qwen
def test_keywords_stage_bucket_only(fake_env): ...  # "fortigate vpn" → work\net
def test_suggest_returns_local_first_then_updates_once(fake_env): ...  # on_update called exactly once with source "qwen"
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(launcher): concurrent suggestion pipeline (qwen, claude, history, keywords)`.

---

### Task 7: Launch commands and live focus

**Files:**
- Create: `launcher/launch.py`, `tests/launcher/test_launch.py`

**Interfaces:**
- Consumes: `Row`, `Index.record_launch`, `termfocus.focus_session(pid, cwd) -> dict`.
- Produces: `WRAPPERS = ("claude","clauded","claude6","claudeb","claudew","qwen")`; `default_wrapper(root, cwd: Path) -> str` (`cc` area → `"claude"`, else `"clauded"`); `resume_command(row: Row, wrapper: str|None) -> Launch`; `new_command(cwd: Path, name: str, wrapper: str) -> Launch`; `@dataclass Launch: cwd: Path, argv: list[str], warnings: list[str]` where `argv` is the wrapper name followed by flags (`["clauded","-r",sid]`, `["clauded","-n",name]`, `["qwen","-r",sid]`); `to_shell_result(launch) -> str` JSON `{"cd": str, "run": argv, "warnings": [...]}` for the PowerShell function (PowerShell does the `Set-Location` and splats `run`, so no shell quoting happens in Python); `focus_live(row) -> dict` delegating to `termfocus.focus_session`.
- Wrapper choice for resume: `row.wrapper` → `default_wrapper` → override argument.

- [ ] **Step 1: Write the failing tests**

```python
def test_resume_uses_recorded_wrapper(...): ...
def test_resume_qwen(...): assert launch.argv == ["qwen","-r","q1"]
def test_new_command_keeps_name_verbatim(...):
    assert new_command(p, 'fix "auth" $env thing', "clauded").argv == ["clauded","-n",'fix "auth" $env thing']
def test_missing_cwd_adds_warning(...): ...
def test_focus_live_delegates(monkeypatch): ...
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(launcher): resume and new-session launch commands`.

---

### Task 8: Terminal surface, resume list and preview

**Files:**
- Create: `launcher/tui/__init__.py`, `launcher/tui/app.py`, `launcher/tui/resume.py`, `launcher/tui/preview.py`, `launcher/cli.py`, `tools/cc.ps1`, `tests/launcher/test_tui_resume.py`

**Interfaces:**
- Consumes: `Index`, `display.*`, `launch.*`, `stores.full_last_reply`.
- Produces: `cli.main(argv) -> int` with subcommands `cc` (list), `cc new [name]`, `cc index` (refresh only, prints counts); exit code 0 and **one JSON line on stdout** when a launch is chosen (`to_shell_result`, after calling `Index.record_launch(store, sid, wrapper)` for a resume, and for a new session once the sid is known: the cli passes `--session-id <uuid4>` it generated so the wrapper can be recorded up front), nothing on stdout otherwise (all UI goes to the alternate screen); `cc --json` prints rows as JSON for the browser or scripts.
- `resume.py`: prompt_toolkit full-screen `Application`; a pure function `render_rows(rows, width, now, selected) -> list[tuple[str,str]]` (style, text) producing columns `age(4) name(28) location(fill, middle_ellipsis) tool(7) live(5)`, header and footer lines exactly as the round-3 mockup; keys `↑↓`, typing filters, `space` preview, `enter` resume/focus, `n` new, `o` open folder (`explorer.exe`), `w` cycle wrapper, `q`.
- `preview.py`: pure `render_preview(row, full_reply, width) -> str` (name line, path line, first ask, last ask, last reply, wrapped at width-2, footer); shown in a scrollable `TextArea`; `space`/`esc` back.
- `tools/cc.ps1`: defines `function cc { $r = & "<venv python>" -m launcher.cli @args; if ($LASTEXITCODE -eq 0 -and $r) { $j = $r | ConvertFrom-Json; Set-Location $j.cd; & $j.run[0] @($j.run[1..($j.run.Count-1)]) } }` with the venv path resolved relative to the script; the user dot-sources it from the profile.

- [ ] **Step 1: Write the failing tests** on the pure renderers (no terminal needed):

```python
def test_render_rows_columns_and_ellipsis(fake_env): ...
def test_render_rows_live_marker_and_order(fake_env): ...
def test_render_preview_contains_three_sections_and_wraps(fake_env): ...
def test_cli_json_lists_rows(fake_env, capsys): ...
def test_cli_prints_single_json_line_on_choice(fake_env, capsys, monkeypatch): ...  # app stubbed to return a Launch
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS; manual check in Windows Terminal: `python -m launcher.cli` against the real store lists in under 1 s after the first index (`cc index` first), preview opens full screen and returns.
- [ ] **Step 5: Commit** `feat(launcher): terminal resume list with full-screen preview`.

---

### Task 9: Terminal surface, new-session walk

**Files:**
- Create: `launcher/tui/new.py`, `tests/launcher/test_tui_new.py`
- Modify: `launcher/tui/app.py`, `launcher/cli.py`

**Interfaces:**
- Consumes: Task 4 tree, Task 5 scaffold, Task 6 `suggest`, Task 7 `new_command`.
- Produces: a state machine `NewFlow(env, index, runner, http)` with pure transitions, tested without a terminal: `start(name) -> State`, `State.folder`, `State.rows` (children + `● start here` + `+ new folder here…` per level rules), `type_filter(text)`, `enter()`, `back()`, `esc()` (to root), `apply_suggestion(s)` (jump only if the user has not moved; else insert highlighted row at top), `choose_new_folder(name, kind)`, `choose_wrapper(w)`, `choose_github(acct)`, `result() -> Launch|None`. Prompt order per spec: name → picker (suggestion live) → new folder name (prefilled `slug(name)`) → kind (options from `new_folder_kind_options`) → wrapper (default per area) → GitHub account (only for kind `project`, default personal).
- "Start here" hidden on `root`; shown at every other level.

- [ ] **Step 1: Write the failing tests**

```python
def test_rows_at_bucket_level_offer_start_and_new(fake_env): ...
def test_new_folder_under_work_bucket_is_organisational(fake_env): ...
def test_new_folder_under_clients_is_client(fake_env): ...
def test_new_folder_deep_asks_project_or_container_default_project(fake_env): ...
def test_suggestion_jumps_when_user_has_not_moved(fake_env): ...
def test_suggestion_is_offered_not_forced_after_user_moved(fake_env): ...
def test_fragment_jump(fake_env): ...  # "cl/ba/bi"
def test_result_launch_for_new_project(fake_env): ...  # argv ["clauded","-n",name], folder created, plan.md exists
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** `NewFlow` and its prompt_toolkit screens. **Step 4: Run** → PASS; manual: `cc new` against the real tree with VPN up shows the Qwen suggestion within ~1 s.
- [ ] **Step 5: Commit** `feat(launcher): new-session walk with live suggestion`.

---

### Task 10: Browser backend (`Api` methods)

**Files:**
- Modify: `claude_sessions.py` (class `Api` around line 4148; `_sessions` around 4292)
- Create: `tests/launcher/test_browser_api.py`

**Interfaces:**
- Consumes: `Index`, `tree.children`, `suggest`, `scaffold.create_folder`, `launch.new_command`.
- Produces on `Api`: `launcher_rows() -> list[dict]` (Index rows as dicts, `tool` key), `launcher_children(rel: str) -> list[dict]`, `launcher_kind_options(rel) -> list[str]`, `launcher_suggest(name) -> dict` (local answer; starts the model stages and stores the first model answer under a token), `launcher_suggest_poll(token) -> dict|None`, `launcher_new(payload: dict) -> dict` (`{name, rel, new_folder, kind, wrapper, github}` → scaffold → launch in a **new Windows Terminal tab** using the same `wt -d <dir> cmd /k …` shape as `resume_session`, with `-n <name>`; returns `{ok, path, warnings}`).
- `_sessions()` gains `tool: "claude"` on existing rows and **appends Qwen rows** from the Index (`tool: "qwen"`, `id`, `display_title`, `cwd`, `mtime`, `when`, message counts); `Api.resume` routes `tool == "qwen"` rows to `qwen -r <sid>` through the same terminal launcher.
- Launching is done through the existing `Popen` argument lists (never `shell=True`); the session name is passed as one argv element.

- [ ] **Step 1: Write the failing tests** (import `claude_sessions` with `pytest.importorskip("webview")` as `test_session_index.py` does; monkeypatch `Popen`):

```python
def test_sessions_include_qwen_rows_with_tool(fake_env, monkeypatch): ...
def test_launcher_new_launches_wt_with_name_argv(fake_env, monkeypatch): ...
def test_resume_qwen_row_uses_qwen_r(fake_env, monkeypatch): ...
def test_launcher_suggest_returns_local_then_poll(fake_env, monkeypatch): ...
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS; whole suite passes.
- [ ] **Step 5: Commit** `feat(browser): launcher API, Qwen rows in the session list`.

---

### Task 11: Browser front end (tool column, New session dialog)

**Files:**
- Modify: `claude_sessions.py` (`HTML_TEMPLATE` from ~5813: CSS, the `render()` function ~7239, toolbar), `i18n.py` (new strings, German key + English)

**Interfaces:**
- Consumes: Task 10 `Api` methods via `window.pywebview.api`.
- Produces: list rows show a `tool` badge (`claude`/`qwen`) and a tool filter chip; toolbar button "New session" opening a dialog identical in structure to the round-3 mockup (`launcher-mockups-v3.html`, tab "New: browser columns"): name field; suggestion chip (polls `launcher_suggest_poll` every 500 ms until answered or 20 s); Miller columns fed by `launcher_children`, each column ending with "+ New folder…", a "Start here" button; new-folder sub-form (name prefilled with slug, kind radio from `launcher_kind_options`, GitHub chips personal/work when kind is project); wrapper chips; "Create and start" calls `launcher_new` and shows warnings.
- i18n: every new UI string added as a German key with English translation per `i18n.py` conventions (the i18n check in `build.ps1` must pass).

- [ ] **Step 1: Add the strings to `i18n.py`; run the i18n check from `build.ps1` (the command it uses) → passes.**
- [ ] **Step 2: Implement the CSS/JS/HTML in `HTML_TEMPLATE`.**
- [ ] **Step 3: Manual verification** (the only UI test): run `python claude_sessions.py`, confirm Qwen rows appear with badges, open New session, type a name, see the suggestion chip update, walk the columns, create a project under a temporary `CC_LAUNCHER_ROOT` and see a Windows Terminal tab open with `-n`. Record the steps and result in the task report.
- [ ] **Step 4: `python -m pytest -q tests` → PASS.**
- [ ] **Step 5: Commit** `feat(browser): tool badge and New session dialog`.

---

### Task 12: Packaging, docs, release

**Files:**
- Modify: `build.ps1`, `ClaudeSessionBrowser.spec` (hidden imports for `launcher`, `prompt_toolkit`), `README.md`, `plan.md`, `setup.iss` (install `cc.exe` and `tools/cc.ps1`)
- Create: `cc.spec` (console, onefile, entry `launcher/cli.py`, name `cc`)

- [ ] **Step 1: Add the `cc.exe` console build to `build.ps1` after the main build; `setup.iss` installs it next to `ClaudeSessionBrowser.exe` and `tools\cc.ps1` into the app folder; `cc.ps1` resolves the python/exe: prefer `cc.exe` beside it, else the venv.**
- [ ] **Step 2: Run `.\build.ps1`** → `dist\cc.exe` exists; `dist\cc.exe --json` lists rows; `dist\ClaudeSessionBrowser\ClaudeSessionBrowser.exe` starts.
- [ ] **Step 3: README section "cc launcher"** (install the profile line `. "<app dir>\cc.ps1"`, keys, how suggestions work, env overrides); `plan.md` updated with the launcher status.
- [ ] **Step 4: Commit** `chore: package cc.exe, docs`.
- [ ] **Step 5: Hand-over, controller only:** merge `launcher` into `main` after the final review (fast-forward if possible), then the user runs `.\release.ps1 -Version 1.7.0.0 -Notes "cc launcher: unified Claude+Qwen list, New session, terminal cc" -Publish`. Subagents never run `release.ps1` or push.
