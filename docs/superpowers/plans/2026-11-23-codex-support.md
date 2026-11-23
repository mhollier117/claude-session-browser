# Codex Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** List, resume and track OpenAI Codex CLI sessions in the Session Browser and `cc`, and show Codex sessions and Codex usage on both Clawdmeters (C6 AMOLED 2.16 and 7") next to Claude's, with no firmware change.

**Architecture:** A new top-level `codex_store.py` reads Codex's own files (read-only): rollouts, titles, liveness, usage. The launcher index gains a third store, `codex`. `SessionsService` merges Codex live sessions into the Claude list. The Clawdmeter link sends Codex sessions in the existing `se` list and Codex usage as one extra `ac` row placed after the Claude accounts.

**Tech Stack:** Python 3.12 (`.venv`), sqlite3, psutil, pytest; pywebview front end in `claude_sessions.py`.

**Spec:** No separate document. The decisions were made in the Plannotator Inbox thread "Add Codex to Session Browser + Clawdmeters" (2026-11-23) and are listed below. Executors take them as requirements.

## Decisions (the spec)

1. The devices show both Codex sessions and Codex usage.
2. App first, no firmware flash. Everything must work on the firmware flashed today (`csb-buddy`, protocol 2).
3. Only Codex **terminal** sessions are listed: rollouts whose `session_meta.payload.source == "cli"` (a string). Excluded: sub-agents (`source` is an object), VS Code/desktop threads (`source == "vscode"`), and Codex's imports of Claude sessions (ids in `~/.codex/external_agent_session_imports.json` → `records[].imported_thread_id`).
4. A Claude account and Codex can be active at the same time. Codex never replaces the Claude usage, the Claude limit notifications or the Claude live account. Both show side by side, in the app and on the devices.
5. Codex is not added to the Accounts tab (it has no account switching).
6. Out of scope, stated so nobody builds it: starting a *new* Codex session from the New-session dialog or `cc new`, Codex limit notifications, a firmware Codex gauge or badge.

## Global Constraints

- Never write to `~/.codex` (or `CODEX_HOME`). Never open or lock files in `thread-writer-locks`: only `stat` them.
- Open `state_5.sqlite` only as `sqlite3.connect("file:<path>?mode=ro", uri=True)`. Any `sqlite3.Error` → behave as if the file were missing. The file name carries a schema version, so a missing file is normal.
- Read rollouts only with Python `open(..., "rb")`, bounded head/tail reads (the live rollout here is 140 MB). Never use PowerShell `Get-Content`/`Select-String` on them (CLAUDE.md).
- Codex home: `os.environ.get("CODEX_HOME")` or `~/.codex`.
- Resume command: `codex --dangerously-bypass-approvals-and-sandbox resume <id>` in bypass mode (the launcher default, as `clauded`). `codex resume <id>` when asking permissions (ctrl-p). Launch only through `launch_in_terminal` (app) / the `cc.ps1` result (launcher).
- Device text is ASCII (`clawdmeter._ascii`). Codex session names on the device get the prefix `cx ` (the device has no badge). The Codex `ac` row nickname is `Codex`.
- i18n: every new `t("...")` is one German string literal with its English in `i18n.py` `TRANSLATIONS["en"]`. `tools\check_i18n.py` must pass.
- Tests: `$env:TEMP = $env:TMP = "$env:LOCALAPPDATA\Temp"; .venv\Scripts\python.exe -m pytest -q tests`.
- Index schema: `_SCHEMA_VERSION` 3 → 4 (`index-v4.sqlite`), because an older build would read `codex` rows as Claude rows and resume them with `claude -r`.

## Review Focus

1. `state_5.sqlite` locked, mid-migration or renamed (e.g. `state_6.sqlite`) → titles fall back to `session_index.jsonl`, then the first ask. No exception reaches the list. Test in Task 1.
2. Usage whose `resets_at` has passed → that window reads 0 % with no reset time, never the old percentage. Test in Task 1.
3. No Claude account is live (or there are no Claude accounts) while Codex usage exists → the Codex `ac` row is sent with `l: 0`. Otherwise the firmware's "first live account" (`ui.cpp:719 live_account_index`, `ui_wide.cpp:2008`) would put the Codex nick on the Claude usage. A switch request for that row is ignored. Test in Task 5.
4. Two Codex processes started seconds apart, one opened with the picker (`codex --yolo resume` with no id) → each process gets at most one thread and no thread is live twice. A process with an id on its command line takes that id. Test in Task 1.
5. Six Claude accounts plus Codex → Claude accounts are capped at 5, so the Codex row is always sent (firmware `MAX_ACCOUNTS 6`). Test in Task 5.

---

### Task 1: `codex_store.py`, reading Codex's files

**Files:**
- Create: `codex_store.py`
- Test: `tests/test_codex_store.py` (build fake homes in `tmp_path`; helper `write_rollout(dir, id, records, source="cli")` in the test file)

**Interfaces:**
- Produces:
  - `codex_home() -> Path`
  - `imported_ids(home: Path) -> set[str]`
  - `read_meta(path: Path) -> dict | None`: the `payload` of the first line when its `type == "session_meta"`, else None.
  - `is_terminal(meta: dict, imported: set[str]) -> bool`: `isinstance(meta.get("source"), str) and meta["source"] == "cli" and meta.get("id") not in imported`.
  - `terminal_rollouts(home: Path) -> dict[str, Path]`: id → path over `sessions/**/rollout-*.jsonl`, terminal only. When an id has several files, the newest mtime wins.
  - `titles(home: Path) -> dict[str, str]`: sqlite `threads.title` (non-empty), overridden by `session_index.jsonl` `thread_name` (the last line per id wins).
  - `@dataclass CodexTail: last_ts: float | None; turn_open: bool; limit_hit: bool; rate_limits: dict | None; rate_ts: float | None; ctx_tokens: int | None; ctx_window: int | None; model: str | None`
  - `read_tail(path: Path, tail_bytes: int = 262_144) -> CodexTail`
  - `usage(home: Path, now: float, recent: int = 8) -> dict | None`
  - `live_threads(home: Path, procs: list[dict] | None = None) -> dict[str, int]`: id → pid. `procs` items are `{"pid": int, "create_time": float, "cmdline": list[str]}`. The default reads `codex.exe` processes with psutil.

- [ ] **Step 1: Write the failing tests**

```python
def test_is_terminal_filters_subagents_vscode_and_imports(tmp_path): ...
    # source "cli" -> True; {"subagent": {...}} -> False; "vscode" -> False; id in imported -> False
def test_terminal_rollouts_newest_file_wins(tmp_path): ...
def test_titles_session_index_overrides_sqlite(tmp_path): ...
def test_titles_survive_missing_or_corrupt_sqlite(tmp_path):            # Review Focus 1
    (home / "state_5.sqlite").write_bytes(b"not a database")
    assert titles(home) == {"t1": "From index"}
def test_read_tail_turn_open_until_task_complete(tmp_path):
    # task_started after the last task_complete -> turn_open True; then task_complete -> False; turn_aborted -> False
def test_read_tail_rate_limits_and_context(tmp_path):
    # token_count with info.last_token_usage.input_tokens=141445, model_context_window=475000,
    # rate_limits.primary {used_percent: 12.0, window_minutes: 10080, resets_at: <now+3600>}
    assert t.ctx_tokens == 141445 and t.ctx_window == 475000 and t.rate_limits["primary"]["used_percent"] == 12.0
def test_read_tail_limit_hit_when_rate_limit_reached_type_set(tmp_path): ...
def test_read_tail_skips_partial_last_line(tmp_path): ...
def test_usage_maps_windows_by_length(tmp_path):
    # primary window 10080 -> w=12, w_reset_at=resets_at; secondary None -> s None; plan "prolite"
    assert u == {"s": None, "s_reset_at": 0.0, "w": 12, "w_reset_at": R, "limited": False, "plan": "prolite", "at": TS}
def test_usage_window_300_is_five_hour(tmp_path): ...
def test_usage_expired_window_reads_zero(tmp_path):                    # Review Focus 2
    # resets_at < now -> w == 0 and w_reset_at == 0.0
def test_usage_takes_newest_record_by_timestamp_across_rollouts(tmp_path): ...
def test_usage_none_without_token_count(tmp_path): ...
def test_live_threads_id_from_cmdline(tmp_path):
    procs = [{"pid": 10032, "create_time": T, "cmdline": ["codex.exe", "--yolo", "resume", "01a1ef10-2463-7ec2-828a-163001599fdd"]}]
def test_live_threads_lock_mtime_matches_picker_process(tmp_path):     # Review Focus 4
    # two procs 5 s apart, no id; locks at start+6 s and start+5+3 s -> each pid one id, no id twice
def test_live_threads_ignores_app_server_processes(tmp_path): ...       # cmdline contains "app-server"
def test_live_threads_ignores_lock_without_process(tmp_path): ...
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest -q tests/test_codex_store.py`
Expected: FAIL, `ModuleNotFoundError: codex_store`.

- [ ] **Step 3: Implement `codex_store.py`**

Rules the tests do not spell out:
- `read_tail`: read backwards from EOF in doubling steps capped at 16 MB (as `launcher/stores.py _tail`). Drop the first, partial line of a mid-file read and a last line without `\n`. `last_ts` comes from the top-level `timestamp` (ISO, `Z` = UTC). Event types: `event_msg` with `payload.type` in `task_started`, `task_complete`, `turn_aborted`, `token_count`. `limit_hit` = the latest `rate_limits.rate_limit_reached_type` is not None, or any window's `used_percent >= 100`.
- `usage`: look at the `recent` most recently modified terminal rollouts and take the `rate_limits` with the newest `rate_ts`. Each of `primary` and `secondary` maps by `window_minutes`: `<= 360` → `s`, otherwise → `w`. `limited` = that record's `limit_hit`.
- `live_threads` without an id on the command line: match the lock file (`thread-writer-locks/<id>.lock`, skip `.coordination.lock`) whose mtime falls in `[create_time - 5, create_time + 60]` and is closest to `create_time`. Assign greedily by smallest gap so that each lock and each process is used once. Only ids that pass `terminal_rollouts` count.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest -q tests/test_codex_store.py`
Expected: all PASS.

- [ ] **Step 5: Commit**

```powershell
git add codex_store.py tests/test_codex_store.py
git commit -m "feat(codex): read Codex sessions, titles, liveness and usage from ~/.codex"
```

---

### Task 2: Launcher index, a `codex` store, and resume

**Files:**
- Modify: `launcher/paths.py` (`Env.codex_home`, `default_env`)
- Modify: `launcher/stores.py` (Codex record parser, per-store prefilter regex, `read_codex`)
- Modify: `launcher/index.py` (`_SCHEMA_VERSION = 4`, `BACKENDS`, `_BACKEND_LABEL`, `backend_of`, `type_label`, `_transcripts`, `_live`, `_titles`)
- Modify: `launcher/launch.py` (`resume_command` for `store == "codex"`)
- Modify: `launcher/tui/app.py:39` (colour for `codex`)
- Modify: `README.md` (cc launcher section: Codex rows, `CODEX_HOME` in the overrides table, the index rebuild on first run)
- Test: `tests/launcher/test_index_codex.py`, `tests/launcher/test_launch_codex.py`; update `tests/launcher/launcher_helpers.py` so test `Env`s get a `codex_home`.

**Interfaces:**
- Consumes: `codex_store.terminal_rollouts`, `read_meta`, `titles`, `live_threads` (Task 1).
- Produces:
  - `Env(root, claude_home, qwen_home, data_dir, codex_home: Path, now=...)`. `codex_home` is a field **with a default** (`Path.home() / ".codex"`), so existing `Env(...)` calls keep working.
  - `stores.read_codex(path: Path, tail_bytes: int = 262_144, count: bool = True) -> SessionFacts` with `store="codex"`, `sid` = the meta `id` (not the file stem) and `cwd` = the meta `cwd`.
  - `Row.store == "codex"`, `Row.backend == "codex"`, `type_label(row) == "codex"`.
  - `launch.CODEX_BYPASS_FLAG = "--dangerously-bypass-approvals-and-sandbox"`
  - `resume_command(codex_row, None, root=r).argv == ["codex", CODEX_BYPASS_FLAG, "resume", sid]`; with `ask_permissions=True`: `["codex", "resume", sid]`. An explicit `wrapper` is ignored for Codex rows.

- [ ] **Step 1: Write the failing tests**

```python
def test_codex_rows_indexed_with_meta_id_and_cwd(tmp_path): ...
    # rollout-2026-11-18T09-29-34-<id>.jsonl -> Row.sid == <id>, Row.cwd == meta cwd, store "codex"
def test_codex_subagent_and_import_rollouts_not_indexed(tmp_path): ...
def test_codex_first_and_last_ask_from_user_message_events(tmp_path):
    # event_msg user_message "fix the build" / agent_message "done" -> first_ask "fix the build", last_reply "done"
    # response_item message role=user with <environment_context> is NOT the ask
def test_codex_counts_only_user_and_agent_message_events(tmp_path): ...
def test_codex_display_name_from_codex_titles(tmp_path): ...
def test_codex_live_pid_from_live_threads(tmp_path): ...   # inject via monkeypatch on codex_store.live_threads
def test_type_label_and_backend_codex(): ...
def test_index_file_is_v4(tmp_path): assert index_file(tmp_path).name == "index-v4.sqlite"
def test_resume_codex_bypass_and_ask(): ...
def test_resume_codex_ignores_explicit_wrapper(): ...
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest -q tests/launcher/test_index_codex.py tests/launcher/test_launch_codex.py`
Expected: FAIL.

- [ ] **Step 3: Implement**

- `stores.py`: add `_codex_record(rec)`. It returns `("user", payload.message)` for `type == "event_msg"` with `payload.type == "user_message"`, `("assistant", payload.message)` for `agent_message`, and None otherwise. Make the byte prefilter per store: `_TYPE_RES = {"claude": <current>, "qwen": <current>, "codex": rb'"type"\s*:\s*"(user_message|agent_message)"'}`, used by `_scan`. `_head`'s cwd comes from the top level, which Codex lacks, so `read_codex` sets `cwd` and `sid` from `codex_store.read_meta`.
- `index.py _transcripts`: add `("codex", sid)` entries from `codex_store.terminal_rollouts(self.env.codex_home)`. `_index_file` already dispatches `read_{store}`. `_live`: add `("codex", id) → _Live(pid, "", False)` from `live_threads`. `_titles`: merge in `codex_store.titles(codex_home)` (UUID keys cannot collide).
- Wrap all Codex reads so that a missing or unreadable `~/.codex` yields no Codex rows and never fails a refresh.

- [ ] **Step 4: Run the launcher tests**

Run: `.venv\Scripts\python.exe -m pytest -q tests/launcher`
Expected: all PASS.

- [ ] **Step 5: Commit**

```powershell
git add launcher tests/launcher README.md
git commit -m "feat(codex): codex store in the session index; resume with codex resume"
```

---

### Task 3: Codex sessions in the Session Browser's list

**Files:**
- Modify: `launcher/bridge.py` (add `codex_session_dicts`)
- Modify: `claude_sessions.py`: `_launcher_build` (~4413, cache `self._launcher_codex`), `_launcher_state` (init it), `_sessions` (~4349, append), `resume` (~4466, `tool == "codex"`), JS `TOOL_STYLED` (~9369) and filter grouping (~9371), the CSS `.tool-badge.codex` (~6790), and the rename/colour guards (~8057, ~8094) that skip `qwencli` (skip `codex` too)
- Test: `tests/launcher/test_browser_api.py` (add cases)

**Interfaces:**
- Consumes: `Row.store == "codex"` (Task 2), `launch.resume_command` (Task 2).
- Produces: `bridge.codex_session_dicts(rows: list[Row], when) -> list[dict]`, the same shape as `qwen_session_dicts` with `"tool": "codex"`.

- [ ] **Step 1: Write the failing tests**

```python
def test_codex_session_dicts_shape():
    d = codex_session_dicts([codex_row], str)[0]
    assert d["tool"] == "codex" and d["id"] == codex_row.sid and d["cwd"] == codex_row.cwd
def test_sessions_include_codex_rows(api_with_index): ...      # follow the existing qwen test's fixture
def test_resume_codex_launches_codex_resume(api, monkeypatch):
    # captured launch_in_terminal argv == ["codex", "--dangerously-bypass-approvals-and-sandbox", "resume", SID]
def test_resume_codex_rejects_bad_id(api): ...
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest -q tests/launcher/test_browser_api.py`
Expected: the new cases FAIL.

- [ ] **Step 3: Implement**

`resume(tool="codex")`: validate the id with `_SESSION_ID_RE`, look the row up with `self._launcher_idx().row("codex", sid)`, and launch `resume_command(row, None, root=env.root).argv` in `_safe_workdir(row.cwd or cwd)`. If the index cannot be read, launch `["codex", CODEX_BYPASS_FLAG, "resume", sid]` in `_safe_workdir(cwd)`. The badge style follows `.tool-badge.qwencli` with its own colour. Codex rows get their own filter button (no "...cli" twin).

- [ ] **Step 4: Run the tests, then look at it**

Run: `.venv\Scripts\python.exe -m pytest -q tests/launcher/test_browser_api.py`
Expected: PASS. Then start the dev copy (`.venv\Scripts\python.exe claude_sessions.py`) and check:
- Codex rows show the `codex` badge.
- The filter works.
- Double-clicking a Codex row opens a pwsh tab running `codex ... resume <id>`.

- [ ] **Step 5: Commit**

```powershell
git add launcher/bridge.py claude_sessions.py tests/launcher/test_browser_api.py
git commit -m "feat(codex): Codex sessions in the browser list, badge, filter and resume"
```

---

### Task 4: Codex live sessions next to Claude's (buddy, focus, voice key)

**Files:**
- Modify: `codex_store.py` (add `CodexLive`)
- Modify: `sessions_live.py` (`SessionsService` merges a second source)
- Modify: `claude_sessions.py` (construct `SessionsService(..., codex=CodexLive())` where the service is created; find it with `SessionsService(`)
- Modify: `voicemode.py` (a Codex session in front → `"h"`)
- Test: `tests/test_codex_live.py`, plus additions to `tests/test_sessions_live.py` and `tests/test_voicemode.py`

**Interfaces:**
- Consumes: `terminal_rollouts`, `titles`, `live_threads`, `read_tail` (Task 1).
- Produces:
  - `codex_store.CodexLive(home: Path | None = None, clock=time.time, procs=None)` with `.snapshot() -> list[dict]`. Each dict has the keys of `LiveSessions._describe`, plus `"tool": "codex"`. `status` is `"busy"` or `"idle"`, `waiting_for` is `""`, `named` is True when a title exists, and `ctx_pct` comes from `sessions_live.context_pct(ctx_tokens, ctx_window)`.
  - `SessionsService(live=None, log=None, codex=None)`. `latest()` returns the Claude snapshot and the Codex snapshot merged, sorted by `(URGENCY[state], -since)`. Every Claude dict without `tool` gets `"tool": "claude"`. If Claude returns None and Codex is empty or absent, `latest()` stays None (the old fallback). A failing Codex snapshot is logged once and treated as `[]`.

- [ ] **Step 1: Write the failing tests**

```python
def test_codex_live_state_working_while_turn_open(tmp_path): ...    # turn_open -> "working"
def test_codex_live_state_limit(tmp_path): ...                      # limit_hit -> "limit"
def test_codex_live_done_idle_sleep_by_age(tmp_path):
    # age < DONE_FOR_S -> "done"; < SLEEP_AFTER_S -> "idle"; else "sleep" (constants from sessions_live)
def test_codex_live_name_title_else_folder(tmp_path): ...
def test_codex_live_never_needs_you(tmp_path): ...                  # no approval signal exists in rollouts
def test_service_merges_and_sorts_by_urgency(): ...
def test_service_none_when_claude_none_and_no_codex(): ...
def test_service_codex_only_when_claude_registry_missing(): ...
def test_voicemode_codex_front_is_h(): ...
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest -q tests/test_codex_live.py tests/test_sessions_live.py tests/test_voicemode.py`
Expected: the new cases FAIL.

- [ ] **Step 3: Implement**

`CodexLive.snapshot` lists only ids from `live_threads`. It caches `CodexTail` per path keyed by `(size, mtime)`, because the service polls every second. Focusing a session from the gadget needs no change: `termfocus.focus_session(pid, cwd)` gets the `codex.exe` pid. Check that by hand in Step 4.

- [ ] **Step 4: Run the tests, then check by hand**

Run the three files. Expected: PASS. With the dev copy running and one Codex terminal open:
- The desktop buddy reacts to a Codex turn.
- A long press on the gadget's Codex session brings its terminal tab forward.

- [ ] **Step 5: Commit**

```powershell
git add codex_store.py sessions_live.py claude_sessions.py voicemode.py tests
git commit -m "feat(codex): Codex live sessions join the buddy and gadget session list"
```

---

### Task 5: Codex usage on both Clawdmeters and in the app, beside Claude's

**Files:**
- Modify: `claude_sessions.py`: `_clawd_extras` (~4790), `_clawd_gadget_sessions` (~4851), `_clawd_switch_request` (~4965), `limit_state` (the method around 5326 that returns `pct`/`wpct`), JS `paintLimit` (~8632)
- Modify: `i18n.py` (the English for the new strings)
- Modify: `plan.md` (a new top section for this work, with the 1.7.0.10/1.7.0.11 state)
- Test: `tests/test_codex_gadget.py` (use the stub-app pattern of `tests/test_clawdmeter_api.py`)

**Interfaces:**
- Consumes: `codex_store.usage` (Task 1), merged sessions with `tool` (Task 4).
- Produces:
  - `self._codex_usage_cached() -> dict | None`: `codex_store.usage(codex_home(), time.time())`, cached for 30 s.
  - The `ac` list = at most **5** Claude accounts, then one Codex row (only when usage is not None): `{"n": "Codex", "l": 1 if any Claude row has l == 1 else 0, "r": 0 if limited else 1, "s": s if s is not None else -1, "sr": mins(s_reset_at), "w": w, "wr": mins(w_reset_at), "f": -1, "fr": 0}`.
  - `self._clawd_codex_index: int | None`, the Codex row's index in the last list built.
  - In `_clawd_gadget_sessions`, sessions with `tool == "codex"` go to `gadget_view` with the name `"cx " + name`. This happens before `gadget_view`, so `_clawd_resolve_session` sees the same names.
  - `limit_state()` gains `"codex": {"spct": int | None, "sreset_at": float, "wpct": int, "wreset_at": float, "hit": bool, "plan": str} | None`.
  - New strings: `t("Codex – Woche")` → "Codex – week", `t("Codex – 5 Stunden")` → "Codex – 5 hours".

- [ ] **Step 1: Write the failing tests**

```python
def test_codex_row_after_claude_accounts_live_when_claude_live(): ...
    # accounts [A(live), B] + codex usage w=12 -> ac[2] == {"n": "Codex", "l": 1, "r": 1, "s": -1, ..., "w": 12, "f": -1, "fr": 0}
def test_codex_row_not_live_without_live_claude_account():            # Review Focus 3
    # accounts [] -> ac[0]["n"] == "Codex" and ac[0]["l"] == 0
def test_claude_accounts_capped_at_five_when_codex_present():         # Review Focus 5
    # 6 Claude accounts -> len(ac) == 6, ac[5]["n"] == "Codex"
def test_six_claude_accounts_without_codex_unchanged(): ...
def test_no_codex_row_without_usage(): ...
def test_codex_sessions_prefixed_cx_on_gadget(): ...
def test_switch_request_to_codex_row_ignored():                       # Review Focus 3
    # _clawd_switch_request(app._clawd_codex_index, "Codex") -> accounts_switch never called; log says ignored
def test_main_usage_payload_stays_claude(): ...                       # _clawd_usage unchanged with codex usage present
def test_limit_state_has_codex_block(): ...
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest -q tests/test_codex_gadget.py`
Expected: FAIL.

- [ ] **Step 3: Implement**

`paintLimit`: after the Claude cards, when `d.codex` is set, add `limitCard(d.codex.spct, d.codex.sreset_at, t('Codex – 5 Stunden'), d.codex.hit)` when `spct` is not null, and `limitCard(d.codex.wpct, d.codex.wreset_at, t('Codex – Woche'), d.codex.hit && d.codex.spct === null)`. This holds on both the Ultra/Fable branch and the normal branch. `_clawd_switch_request`: return before `_clawd_resolve_account` when `index == self._clawd_codex_index`, logging `ignored: Codex has no account switch`.

- [ ] **Step 4: Run everything, then look at both devices**

Run:
- `.venv\Scripts\python.exe tools\check_i18n.py`. Expected: no errors.
- The full suite (Global Constraints). Expected: all pass; rerun `tests/test_name_gate.py` alone if it times out.

Then, with the dev copy connected to each device:
- **C6 2.16:**
  - The account swipe shows a `Codex` row with its weekly % and "active".
  - "Tap to use" is absent on that row.
  - Codex sessions appear in the session swipe as `cx <name>`.
  - The usage screen still shows the Claude numbers.
- **7":** the same on the account cards and the rotation view.

- [ ] **Step 5: Commit**

```powershell
git add claude_sessions.py i18n.py plan.md tests/test_codex_gadget.py
git commit -m "feat(codex): Codex usage beside Claude's in the app and on both Clawdmeters"
```

---

## Release (after review, not part of the tasks)

Fast-forward to `main` and release from the main checkout as in CLAUDE.md: `.\release.ps1 -Version 1.7.1.0 -Notes "Codex sessions, live state and usage" -Publish`. Then confirm with `gh release list --repo mhollier117/claude-session-browser --limit 1`. The first run after the update rebuilds the index (`index-v4.sqlite`), which takes a few minutes.
