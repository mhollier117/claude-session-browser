# Launcher round 2 — usability fixes from real use

> For agentic workers: implement one task per dispatch, TDD, commit per task. Branch `launcher`, worktree
> `C:\claude-code\cc\claude_improvements\csb-launcher`. Python: `C:\claude-code\cc\claude_improvements\claude-session-browser-main\.venv\Scripts\python.exe`.
> Tests: `-m pytest tests/launcher -q` while iterating, `-m pytest -q tests` once before committing (~5 min).
> Never touch the real `~\.claude` / `~\.qwen` in tests (use `fake_env`, `launcher_helpers`). Do not push. No attribution trailer lines in commit messages.

**Goal:** fix what a week of real use showed: the suggester is slow and often wrong, the picker loses typed input and hides its exits, the list order is wrong, and the new-session flow asks too much.

**Evidence** (`%LOCALAPPDATA%\cc-launcher\suggest.log`, 2026-11-05..11): Qwen usable 8/20 at ~4 s (7 empty `{}` answers, 4 unreachable timeouts); Claude 18/18 valid at ~13 s but over-matches on weak shared words; keywords 0/5; history "9 similar sessions" jumped the picker into an unrelated folder; the browser ran a full model round per typing pause; nothing records which folder the user finally chose. A read-only A/B on 15 names (reference script: `C:\Users\mhollier\AppData\Local\Temp\claude\C--claude-code\ee343ffe-47d2-4e83-a50b-7da0718de932\scratchpad\qwen-ab\ab_test.py`) showed a retrieve-then-choose prompt gives 15/15 valid at 1.3 s (≈1,000 prompt tokens vs 13,500), but its "same system AND same task" rule creates new folders too eagerly.

## Global constraints

- All existing behaviour not named here stays. The 290 launcher tests keep passing unless a task says a behaviour changes (then update the test and say so in the report).
- Network and subprocess seams stay injectable; tests never call Qwen, `claude.exe`, git or gh.
- The Qwen key is never logged. `suggest.log` stays one JSON line per event.
- Every Claude-launching command clears the child-session marker: the launched command must set `CLAUDE_CODE_CHILD_SESSION` empty and `CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1` (terminal: `tools/cc.ps1` sets both before invoking the wrapper; browser: prefix in the encoded command).
- Index schema changes bump `_SCHEMA_VERSION` (the cache rebuilds itself).

---

### Task 1: Suggester v2 — retrieve, then choose by number

**Files:** create `launcher/retrieve.py`, `tools/suggest_eval.py`, `tests/launcher/test_retrieve.py`; modify `launcher/suggest.py`, `launcher/suggest_prompt.py`, `tests/launcher/test_suggest.py`, `tests/launcher/test_suggest_prompt.py`.

1. `retrieve.py`
   - `candidates(env, index, rows) -> list[Cand]` where `Cand(rel: str, path: Path, depth: int, stats: FolderStats, tokens: frozenset[str])`: every folder to depth 4 below `work` and `home`, depth 2 below `cc` (same walk and same hidden-folder rule as today's prompt tree), tokens from the relative path plus the folder's 3 most recent session names.
   - Tokeniser: lower-case, split on non-alphanumerics, length ≥ 3, minus a stop list (new fix phase update the and for with from into this that review check test issue script setup app tool project work session run add get set use data file files list build make). Stem match: equal, or both ≥ 5 chars and share a 5-char prefix.
   - `rank(name, cands, now, k=14) -> list[Scored(cand, score)]`: score = sum of IDF of each query token that stem-matches a candidate token, + 0.15 when the folder had activity in the last 30 days; IDF over candidate token sets with stem-merged document frequency. Organisational folders (areas, `work\<bucket>`) are never ranked as PROJECT candidates.
   - `places(env, cands, name) -> list[Place(rel, path, note)]`: every area and bucket that exists on disk (so a new bucket like `work\bifrost_automation` appears with no code change), plus `work\clients\<client>` for any client whose name stem-matches a query token. `note` = first non-heading, non-empty line of `<folder>\CLAUDE.md` when present (trimmed to 120 chars), else a built-in note for the known buckets (table in the reference script), else `""`.
   - `strong(scored) -> bool`: top score ≥ 2 distinct rare tokens matched (IDF sum above a threshold the implementer calibrates on the eval set) AND ≥ 1.5× the runner-up. Only a strong local result may move the picker without a model.
2. `suggest_prompt.py`: keep the old prompt exported as `SYSTEM_PROMPT_V1` for reference; add `SYSTEM_PROMPT` v2 and `build_user_message(name, scored, places) -> tuple[str, list[Choice]]` producing a numbered list: `n. PROJECT  <rel>  · <sessions> sessions · recent: "a", "b"` then `n. PLACE    <rel>  — <note>`. Output contract: `{"choice": <n>, "new_folder": "<name or null>", "confidence": "high|medium|low", "reason": "<one sentence>"}`.
   - Rules in the v2 system prompt (write them in plain sentences, with 4 short worked examples drawn from the eval set, including one negative): pick a PROJECT when the session is more work on that same project or system, even if the task is new (a bug in the Halo ticket list sync belongs in the existing ticket-list sync project); a shared generic word alone is not a match; otherwise pick the PLACE that fits and name a new folder (lower case, `_`, 2–4 words, no dates); client work goes under the client; personal or hobby work goes under `home`, not a work bucket; always answer, never `{}`.
3. `suggest.py`
   - `Suggestion` gains `alternatives: list[Path]` (the next two ranked PROJECT candidates that are not the chosen one) and `strong: bool`.
   - Local answer: ranked candidates. `pending.local` is the top candidate with `strong` set per `retrieve.strong`; when nothing scores, `source="root"`. The history stage's "N similar sessions" wording is removed; reason becomes `matches <tokens>`.
   - Remove the keywords stage and `KEYWORDS`.
   - Model stages run **in order, not in parallel**: Qwen first; Claude only when Qwen was skipped (unreachable, no settings) or failed. Both use the v2 prompt. Validation = `choice` is an int in range; PROJECT → `new_folder` forced to None; PLACE → `new_folder` must pass `slug` + `check_name` (if it fails, keep the place and drop the name).
   - Unreachable memory: on a failed TCP connect or `/models` timeout write `data_dir/qwen-unreachable` (timestamp); skip the Qwen stage without any network call for 600 s after that (log `skipped: unreachable (cached)`).
   - `log_outcome(env, name, suggestion, chosen: Path, new_folder: str | None)`: one `suggest.log` line `{"stage": "outcome", "name", "suggested": rel|None, "suggested_source", "chosen": rel, "new_folder", "match": bool}`.
4. `tools/suggest_eval.py` (manual, not a test): runs a table of `(name, expected rel, expected kind)` through the real Qwen endpoint with the v2 prompt and prints a scoreboard (valid, correct folder, correct kind, latency). Seed it with these 15 and their expected answers: Ninja RMM Alert Review → `work\ninja` PLACE; razer synapse app reverse engineer → `home` PLACE; razer app decompile → `home` PLACE; local ai transcription pipeline → `home\ai` PLACE; fortigate vpn cert renewal → `work\net` (PLACE, or PROJECT `work\net\cert issue` also accepted); bifrost vault key rotation runbook → `work\clients\bast\bifrost` PROJECT; Mediko new hire workflow fix → under `work\wf\mediko_user_creation` or `work\clients\Mediko` (either accepted); Halo ticket list sync bug → `work\halo\view_management` PROJECT; CIPP standards drift report → `work\cipp` (PLACE or PROJECT `Cipp Standards`); Keeper SSO rollout script → `work\clients\bast\Keeper` PROJECT; homelab pxe boot order → `home\homelab\pxe` PROJECT; claude session browser launcher review → `cc\claude_improvements\claude-session-browser-main` PROJECT; brandito offboarding shared mailbox → the Rewst client offboarding export or `work\clients\brandito`; contact sync duplicate users → `work\halo\contact_sync` PROJECT; SFDSDF → any PLACE with confidence low. **Run it** (the endpoint is reachable today), iterate on the prompt wording and the `strong` threshold until ≥ 13/15 correct with median latency under 2 s, and paste the final scoreboard in the report. If the endpoint is unreachable, say so and deliver without the calibration.
5. Tests (no network): tokeniser and stem match; IDF ranks a rare-token folder above a generic-token one ("Ninja RMM Alert Review" must not rank a `mediko` workflow folder first in a fixture that has both); `places` picks up a bucket that exists only on disk and reads its CLAUDE.md note; `strong` true/false cases; prompt numbering and that `{}` / out-of-range / non-int choice are rejected; PROJECT forces `new_folder` None; stage order (Claude not called when Qwen answers; called when Qwen fails); unreachable cache skips the network for 600 s using the injected clock; `log_outcome` line shape; key never in the log.

---

### Task 2: Picker — keep what the user typed, never steal the cursor, show the exits

**Files:** modify `launcher/tui/new.py`, `launcher/tui/app.py`, `launcher/cli.py`; create `launcher/flowlog.py`; tests in `tests/launcher/test_tui_new.py`, `tests/launcher/test_flowlog.py`.

1. **Typed text that matches no child becomes a new folder.** In the picker, when the filter is non-empty, contains no `/`, and matches no child, the first row is `+ new folder: <slug(filter)>` (kind `new_typed`) and it is selected; Enter on it goes to the folder-name step prefilled with that slug (not the session-name slug). When the filter matches some children, the `new_typed` row is still offered **after** the children. `● start here` is never the default selection while a filter is typed.
2. **Selection by identity.** Rebuilding rows (offer arrives, filter changes, stats refresh) keeps the selected row by `(kind, path, label-key)`, falling back to the first non-suggestion row. A suggestion row arriving never becomes selected by itself (regression test for the bug where an inserted ★ at index 0 inherited the cursor).
3. **Suggestions block.** The picker opens at the root unless `pending.local.strong`, in which case it opens at that folder as today. At the top of every level it shows up to 3 suggestion rows: the model answer when it arrives (`★ <rel> + new folder: <name>` or `★ <rel>`), then the ranked alternatives (`≈ <rel>`). Enter on one jumps there (and preselects the proposed new folder). Suggestions already equal to the current folder are hidden.
4. **Recent first, long levels folded.** Children are ordered by last activity descending (no-session folders last, alphabetical). When a level has more than 12 children, show the first 12 and a row `… N more (type to filter, enter to show all)`; Enter on it expands for that level. A typed filter always searches all children.
5. **Footer names the exits**, replacing the cramped hint: `enter select · type to filter or name a new folder · backspace up · esc top · /path jump · ctrl-c cancel`, wrapped to the terminal width rather than truncated.
6. **Flow log.** `flowlog.log(env, event, **fields)` appends JSON lines to `data_dir/flow.log` (ts, event, folder rel, row kind, filter text, step); the new flow logs `start`, `open`, `filter-enter`, `new-folder`, `kind`, `launch`, `cancel`. Size cap: when the file exceeds 1 MB keep the last half. At launch call `suggest.log_outcome` (Task 1) with the suggestion in effect and the chosen folder.
7. Tests for each numbered item, including: filter "alert review" in `work\ninja` with no matching child → Enter leads to the folder-name step with `alert_review`, never to `start here`; offer arrival preserves the selected child; >12 children fold and expand; flow log lines written to the fake data dir.

---

### Task 3: Resume list — true last-use order, typing always filters

**Files:** modify `launcher/stores.py`, `launcher/index.py`, `launcher/tui/resume.py`, `launcher/tui/app.py`; tests in `tests/launcher/test_stores.py`, `test_index.py`, `test_tui_resume.py`.

1. `SessionFacts.last_ts: float | None` = the `timestamp` (ISO 8601, Claude) of the last record carrying one in the tail read; Qwen: the equivalent field if present, else None. `Index` stores `last_activity = last_ts or mtime`. Bump `_SCHEMA_VERSION`. Test: a transcript whose file mtime is today but whose last record is dated a month ago sorts by the record date.
2. Keys: every printable character goes to the filter (spaces allowed inside a filter); the letter commands move to chords: `ctrl-n` new, `ctrl-o` open folder, `ctrl-w` cycle wrapper, `ctrl-q` or `ctrl-c` quit; `esc` clears the filter (and does nothing when it is empty); `space` opens the preview only while the filter is empty; `tab` always opens the preview. Footer updated: `↑↓ move · type to filter · tab preview · enter resume · ctrl-n new · ctrl-o folder · ctrl-w wrapper · ctrl-q quit`. Remove the `/`-prefix rule and its tests.
3. Resume from the launcher must not be blocked by the index lock: `record_launch` is best-effort (catch `sqlite3.OperationalError`, add a warning to the result, still print the launch line).

---

### Task 4: Fewer prompts — remembered defaults and one Enter

**Files:** create `launcher/defaults.py`; modify `launcher/tui/new.py`, `launcher/tui/app.py`, `launcher/scaffold.py` (only if needed), `launcher/bridge.py`; tests `tests/launcher/test_defaults.py`, `test_tui_new.py`, `test_bridge.py`.

1. `defaults.py`: `get(env, folder) -> Defaults(wrapper, github)` and `remember(env, folder, wrapper, github)`, stored in `data_dir/defaults.json` keyed by the bucket key (first two relative parts, e.g. `work\wf`, `work\clients`, `home`, `cc`). Built-in fallbacks when nothing is remembered: wrapper per `default_wrapper`; github `"none"` for `work\wf`, `"personal"` elsewhere.
2. GitHub step gains a third option **none** ("no GitHub repo; git init only"). `create_folder(..., github=None)` already does this.
3. Flow: after the folder is settled (start here, or new folder + kind), show one **confirm** step instead of the wrapper and GitHub steps: a summary of what will happen (folder, kind, wrapper, GitHub choice, the exact command) with `enter launch · w change wrapper · g change github · esc back`. `w`/`g` cycle the value in place. Launch remembers the choices for that bucket.
4. `bridge.children_listing` returns `defaults: {wrapper, github}` for the folder so the browser can preselect; `parse_new` accepts `github: "none"`; a successful `launcher_new` calls `defaults.remember`.
5. Tests: defaults fallback table, remember/get round trip per bucket, confirm step transitions, `none` creates no gh call (FakeRunner), bridge payloads.

---

### Task 5: Browser alignment

**Files:** modify `claude_sessions.py` (the `// launcher` script block, `<!-- launcher -->` markup, `Api.launcher_*`), `launcher/bridge.py`, `i18n.py`; tests `tests/launcher/test_browser_api.py`, `test_bridge.py`.

1. Suggestions run **once per finished name**: on Enter in the name field or when it loses focus, not on typing pauses. Remove the 400 ms debounce trigger and the 1.5 s deferred-model timer path from the browser flow (keep `Pending.cancel` for superseded requests).
2. The suggestion chip shows the model answer plus up to two alternative chips (from `Suggestion.alternatives`); clicking any opens the columns there.
3. New-folder form: when the user types in a column filter/new-folder field, that text is the folder name (same rule as the terminal: typed text wins over the session-name slug).
4. Defaults: preselect wrapper and GitHub chips from `defaults`; add the **none** GitHub chip.
5. `launcher_new` writes `flowlog` events and `log_outcome` like the terminal does; the launched command clears the child-session marker and forces persistence (Global constraints).
6. i18n keys for every new string; `tools\check_i18n.py` passes. Manual verification in the real app is done by the controller, not the implementer: do not start the pywebview app, do not open terminal tabs.
