# Session launcher (`cc`) — design

Status: draft for builder check, 2026-11-05. Decisions below were made with the user in the
reorg session (mockups: `cc\launcher-spike\launcher-mockups*.html`, spike: `cc-new-spike.ps1`).

## Intent

One tool, two surfaces, that is the normal way to start or resume a Claude Code or Qwen Code
session on this machine, and that quietly keeps new work inside the reorganised tree
(`C:\claude-code\{work,home,cc}`, see `docs/superpowers/specs/2026-11-02-claude-code-reorg-design.md`
in `cc\reorg`).

- **Resume** must be faster and more informative than `claude -r`: one list across both tools,
  ordered by last use, with name, location and a transcript preview on demand.
- **New** must get from "I want to start on X" to a running, correctly named session in a
  correctly placed folder with the fewest keystrokes, and must never silently create a folder
  in the wrong place.

## Goals, non-goals, anti-goals

Goals
- Terminal command `cc` (PowerShell function wrapping a Python core) and the same features in
  the user's Claude Session Browser fork (`cc\claude_improvements\claude-session-browser-main`,
  pywebview). Shared Python core, two thin surfaces.
- Claude Code and Qwen Code sessions in one list; live sessions shown.
- Launch through the user's existing wrappers (`claude`, `clauded`, `claude6`, `claudeb`,
  `claudew`, `qwen`) so hooks, prelaunch repair and env stay exactly as today.
- Pass the session name at launch (`claude -n "<name>"`) so the `csb-name-gate.ps1`
  UserPromptSubmit hook is satisfied without a rename step.

Non-goals
- Replacing the session browser's existing features (search, repair, delete). The launcher
  adds "New session" and the unified resume list to it; nothing is removed.
- Managing sessions outside `~\.claude` and `~\.qwen`.
- Any change to the reorg migrator or the move map.

Anti-goals (failure even if it "works")
- A session started in a folder the user did not see on screen.
- A new folder created at an organisational level (area or bucket) that is treated as a
  project and gets `git init` / `plan.md`.
- A slow list. Opening `cc` must show the list in under a second after first indexing.
- A model call that blocks the flow: every network stage has a hard timeout and a silent skip.

## Constraints

- Windows 11, PowerShell 7, Python 3.14 (same interpreter the MCP wrappers use:
  `C:\Program Files\Python314\python.exe`). Terminal UI must work in Windows Terminal.
- Session stores are read-only to the launcher except for its own cache. Never write into a
  transcript, `.claude.json`, or `history.jsonl`.
- The user has aphantasia: any UI proposal during the build is shown as an HTML page, not text.
- Lives **inside the Claude Session Browser fork** (`mhollier117/claude-session-browser`,
  working copy `claude_improvements\claude-session-browser-main`, after stage 3
  `cc\claude_improvements\...`), because the user wants all of this baked into the browser
  eventually. Layout: a `launcher/` package holding the core (index, stores, picker model,
  suggestion pipeline, scaffold), a `cc` console entry point for the terminal surface, and
  the browser importing the same package. Work happens on a branch in a git worktree, since
  the fork's main working copy has uncommitted edits from another session (`accounts.py`,
  `tests/test_accounts.py`) that must not be disturbed. This spec moves into that repo's
  `docs/` with the first commit of the branch.
- The installed app (`C:\claude-code\ClaudeSessionBrowser`, built 2026-11-03) is the frozen
  build of that fork, not a second source copy; the terminal `cc` must work from the source
  checkout and from the frozen build's Python alike, or ship as its own small console exe
  built by the existing `release.ps1`.

## The tree the launcher enforces

Levels, from `C:\claude-code`:

| Level | Folders | Kind |
|---|---|---|
| L1 area | `work`, `home`, `cc` | organisational |
| L2 bucket | under `work`: `clients`, `wf`, `rewst`, `halo`, `ninja`, `cipp`, `m365`, `net`, `vendors`, `scripts`; under `home`: `homelab`, `ai`, or a project directly | organisational (`home\<project>` is a project) |
| L3 under `clients` | one folder per client (`bast`, `Mediko`, `brandito`, …) | organisational ("client") |
| below | project folders, nested as deep as the work needs (`clients\bast\bifrost\stackjack_rip_qwen`) | project or container, user's choice |

Rules the UI applies:
- "Start here" (use the current folder as the session's cwd) is offered at every level. The
  tree is read live from disk; nothing is hard-coded except the level kinds above.
- "New folder here…" directly under the root or under `work` creates an **organisational**
  folder (a new area or bucket): no `git init`, no `plan.md`. Under `work\clients` it is a
  **client**: labelled **"a client"** and given a `CLAUDE.md` stub (`# <name>` +
  "Client work for <name>."). Everywhere else, including under a bucket (`work\net`),
  under a client (`work\clients\bast`), and directly under `home` and `cc` (flat:
  `cc\reorg`, `home\LlamaNote` are projects), the user picks **project** (scaffold below,
  default) or **container** (grouping folder only; the walk continues inside it).
  (Amended 2026-11-05 at the final review: the earlier wording made bucket-level folders
  organisational, which left `work\net\<new>` without a scaffold.)
- Project scaffold: `git init`; `plan.md` written from a small template in this repo
  (`templates/plan.md`: title, goal, "Current phase", "Next", "Decisions", "Known issues");
  no project `CLAUDE.md`. Then an initial commit, and the launcher **creates the GitHub repo**:
  a prompt "GitHub account: personal / work" defaulted to **personal** (`mhollier117`, plain
  `gh repo create <name> --private --source . --push`); **work** follows
  `~\.claude\templates\work-github\WORK-GITHUB.md` step by step (the `mhollierBPT` account,
  token-for-one-command pattern, the repo's `CLAUDE.local.md` from that template, never
  `gh auth switch`). Under `work\clients` the prompt still defaults to personal, per the
  user. After the push the launcher writes `.claude-project` with `repo`, `lastCommit` and
  `lastSynced`, so the session-start sync check passes on the first prompt. If `gh` fails
  (offline, name taken) the session still starts; the folder is left with `.git` only and the
  user's session-start rule asks about linking later.
- Folder names: prefilled from the session name, lower-cased, non `[A-Za-z0-9._-]` runs → `_`,
  trimmed; always editable.

## Resume

List columns: relative age (`2h`, `3d`, `Apr`), session name, location, tool (`claude` /
`qwen`), live marker. Location is shown relative to `C:\claude-code` when inside it, else
absolute, with **middle ellipsis** that always keeps the first and the last two path segments
(mockup P1). Sorted by last activity (transcript mtime), live sessions first within the same
age. Typing filters on name, path and the first ask.

Keys: `↑↓` move, `space` full-screen preview, `enter` resume (or focus the window if live),
`n` new session, `o` open the folder in Explorer, `q` quit.

Preview (space): takes over the terminal (alternate screen, like the built-in picker), text
wrapped, scrollable, showing: name, tool, timestamp, message count, size, full path; **first
ask**, **last ask**, **last reply** in full (the whole last reply, not a cap); the exact
command that `enter` will run. `space`/`esc` returns to the list on the same row.

Resume action:
- Claude: in the session's cwd run `<wrapper> -r <sessionId>`. Wrapper choice: the one the
  launcher recorded when it started that session (index column `wrapper`); for sessions it
  did not start, the area default (`work`/`home` → `clauded`, `cc` → `claude`); overridable
  with `w` on the row. The wrappers differ in gateway and model (`claudeb` Bifrost,
  `claudew` BPT gateway, `claude6` pinned model), so the recorded one must win. If the
  session has a display name, pass it again with `-n` so the gate hook stays satisfied on
  resume (builder verifies whether `-r` preserves the name; if it does, omit `-n`).
- Qwen: in the session's `work_dir` run `qwen --resume <sessionId>` (builder confirms the flag
  from `qwen --help`).
- Live: do not start a second copy; bring the owning window to the foreground (best effort:
  find the top-level window of the registered pid or its parent Windows Terminal; if that
  fails, print the pid and the tab title).

Terminal surface mechanics: the Python core prints a one-line JSON result
(`{"cd": ..., "run": [...]}`) and the PowerShell `cc` function performs `Set-Location` and
invokes the wrapper **in the same tab**, so the session starts where the user already is.
The browser opens a new Windows Terminal tab (`wt -w 0 nt -d <dir> pwsh -NoExit -Command
"<wrapper> ..."`).

## New session

Flow (terminal and browser are the same steps):

1. **Session name** (free text). Required; it becomes `-n`.
2. **Suggestion** (see pipeline below) runs with a spinner; the destination picker opens
   **at the suggested folder**, with the proposed new-folder name (if any) preselected as
   `+ new folder: <name>`. `esc` jumps to the root (`work` / `home` / `cc`): that is the plain
   menu path and is always available.
3. **Destination picker**: one folder picker repeated per level. Rows: child folders
   (session count, last activity, tool), `● start here`, `+ new folder here…`. Typing filters;
   a slash-separated fragment (`cl/ba/bi`) resolves each piece against the folders at that
   level and jumps; `backspace` on an empty filter goes up one level. Browser: Miller columns
   with the same rows, `Start here` and `New folder in <x>…` buttons.
4. **New folder here…** → name (prefilled) → kind (project / container / client per the
   level rules) → scaffold accordingly → continue or start.
5. **Launch with**: wrapper chip, defaulted per area, `qwen` available as a tool choice.
6. Start: `mkdir` if needed, scaffold, `cd`, `<wrapper> -n "<name>"`.

## Suggestion pipeline

Stages run in order; the first that returns a usable answer wins; each failure is silent
(logged to the launcher's log only). Total budget 8 s, then stage 3.

| # | Stage | Source | Timeout | Skip when |
|---|---|---|---|---|
| 1 | Self-hosted Qwen | OpenAI-compatible endpoint from `~\.qwen\settings.json`: `model.baseUrl`, key from `settings.env[<provider envKey>]`. **Model id: the `-current` alias** (`qwen3-vl-current`, listed by `GET /v1/models`), never the configured `model.name`, so the suggestion never makes the server load a different model than the one already serving; fall back to `model.name` only if no `*-current` id is listed. Request `chat_template_kwargs: {enable_thinking: false}`, `temperature: 0`, `response_format: {type: json_object}`, `max_tokens: 400` | 1 s TCP connect, 6 s total | settings missing, host unreachable (off VPN), non-JSON answer |
| 2 | Haiku via the signed-in Claude Code session | `claude.exe -p "<user msg>" --model haiku --output-format json --system-prompt "<system>" --strict-mcp-config --mcp-config '{"mcpServers":{}}' --tools ""`, run from `%LOCALAPPDATA%\cc-launcher\suggest-cwd` so its transcripts land in one project folder the index ignores (builder checks for a no-persistence flag). No API key; billed to the subscription | 20 s, **in parallel** with stage 1, never in sequence | `claude.exe` missing, non-zero exit, `is_error`, non-JSON |

Measured 2026-11-05 over VPN against `qwen3-vl-huihui` (SGLang, port 30000): TCP connect
29 ms; with thinking off, 4 of 4 test names placed correctly in 0.96 to 1.19 s each (match
to an existing deep folder, new folder under an existing client, new folder under a bucket,
new folder under `home\ai`). With thinking on the same prompt took 9.4 s. With the first,
looser prompt and thinking off, the model proposed a new folder beside an existing match;
the "decide in this order" wording below fixed that. Thinking stays off.
| 3 | History | word overlap between the name and existing session names + folder names; folder of the best cluster | local | no overlap |
| 4 | Keyword table | small per-bucket list (forti/vpn/cert → net, halo/runbook → halo, pxe/supermicro → homelab, …); bucket only | local | no match |
| 5 | Root | picker opens at `C:\claude-code` | — | — |

A suggestion is shown with its origin and reason ("suggested by Qwen · 2 similar sessions
live here"); it is never applied without the user confirming in the picker.

**Non-blocking.** Measured 2026-11-05: the bare `claude.exe -p` call takes 12 to 19 s wall
time here, of which only 1.4 to 3.8 s is the API; the rest is CLI start-up. That cannot sit
in a sequential chain. So the picker opens immediately on the best local answer (stage 3,
then 4, then 5) with a "suggesting…" marker, while stages 1 and 2 run concurrently; the first
model answer that validates is inserted as a highlighted row at the top of the current level
(or the picker jumps to it if the user has not moved yet). Qwen on VPN answers in about 1 s,
so in practice the jump happens before the user reads the list; off VPN the Haiku answer
arrives while they are already walking and is offered, not forced. A second model answer
after the first is discarded.

Qwen thinking modes, same test set: thinking off 0.96 to 1.19 s, 4/4 correct;
`reasoning_effort: "medium"` 4.1 to 7.8 s, and one answer named a folder that does not exist
(`work\vendors\keeper` with `new_folder: null`), which the validator would have rejected;
`thinking_budget` variants were not honoured (one call hit the 1,500-token cap with no
content). Thinking stays off.

### Prompt (stages 1 and 2, same content)

The prompt must carry the structure, the rules and real examples, not just the name. System
message:

```
You place a new coding-assistant session into an existing folder tree. Reply with JSON only.

Tree root: C:\claude-code. Levels:
- L1 areas: work (paid work for the MSP), home (personal: homelab, local AI, hobby projects), cc (tooling for Claude Code itself: session repair, session browser, launcher, skills, reorg).
- L2 buckets under work: clients (per-client folders, one per customer), wf (Rewst workflow exports, folder per export name and date), rewst (Rewst projects not tied to one export), halo (HaloPSA projects), ninja (NinjaOne scripts), cipp (CIPP), m365 (Microsoft 365 / Entra / Intune), net (Fortinet, VPN, certificates, DNS, firewalls), vendors (vendor tooling: Huntress, Keeper, Keepit, Vircom, Blackpoint, Proofpoint, ScreenConnect), scripts (one-off scripts).
- Under home: homelab (PXE, Supermicro, fgt2, Traefik, the lab network), ai (local models, Qwen, Ollama), or a project folder directly under home.
- Under clients: one folder per client, then project folders, nested as deeply as the work needs.
- L1, L2 and clients\<client> are organisational: a session is rarely started directly in them.

Decide in this order:
1. MATCH: look for an existing folder in the tree whose own name, or whose recent session names, share a distinctive word with the session name (ignore words like new, fix, phase, update). If one exists, return exactly that folder as path and set new_folder to null. Prefer the deepest match with the most recent activity. Do not propose a new folder when a match exists.
2. PLACE: only if nothing matches, choose the best parent (the client folder, named exactly as it exists, if the work is for a customer; else the bucket) and propose ONE new folder name: lower case, words joined by _, no dates unless the parent uses them (wf does).
3. Never propose a folder directly under the root.
confidence: high = step 1 matched; medium = step 2 with a clear parent; low = otherwise.

Output JSON: {"path": "<relative to C:\claude-code, existing folder>", "new_folder": "<name or null>", "confidence": "high|medium|low", "reason": "<one sentence naming the matched folder or the chosen parent>"}
```

The tree listing must include empty buckets (`work\vendors · 0 · —`) so PLACE has
somewhere to put new work; it was the presence of `home\ai` and `work\vendors` as empty rows
that made the Keeper and Ollama test cases land correctly.

User message, built per call:

```
Session name: <name>

Folder tree (depth 4 under work and home, depth 2 under cc; each line: path · sessions · last activity · up to 3 recent session names):
work\clients\bast\bifrost · 7 · 1d · "bifrost planning", "stackjack_rip_qwen-8b", "token refresh retry"
work\net\fortinet_api · 2 · 2h · "FortiOS 7.6 API auth"
...

Most recent 15 sessions overall (name → folder):
"homelab pxe boot order" → home\homelab\pxe
...
```

Builder notes: cap the tree at 400 lines (drop empty, oldest folders first); strip `.git`,
`node_modules`, `_psmodules` and dot-folders; for Haiku send the schema through
`output_config.format` and parse `response.content[0].text`; for the Qwen endpoint request
`response_format: {"type": "json_object"}` and fall back to extracting the first `{...}`
block. Validate: `path` must exist under the root and not be `.`; `new_folder` must pass the
slug rule; otherwise treat as failure and fall through. Log prompt, answer and latency to
`%LOCALAPPDATA%\cc-launcher\suggest.log` for tuning.

## Index

`%LOCALAPPDATA%\cc-launcher\index.sqlite`. One row per session (Claude: each
`~\.claude\projects\<enc>\<sid>.jsonl` that is a top-level session, not `agent-*`,
compaction or subagent files; Qwen: `~\.qwen\projects\<enc>\chats\<sid>.jsonl` with its
`.runtime.json`). Columns: store, session id, cwd (from the transcript's own `cwd`, falling
back to the folder encoding via the reorg tool's `reorg.sessions` resolver), display name,
first ask, last ask, last reply (first 400 chars; the full text is read on preview), message
count, size, mtime, last activity, live pid. Refresh rule: rescan file list on every launch
(cheap), re-parse only rows whose size or mtime changed; first ask comes from the head, last
ask/reply from the tail (read the last 256 KB, extend backwards if no user+assistant pair is
found). Live sessions from `~\.claude\sessions\<pid>.json` and `~\.qwen\sessions\<pid>.json`,
checked against running pids. Name resolution reuses the session browser fork's existing
logic (same records it reads today); the builder lifts it into the core rather than duplicating.

## Left to the builder

- Terminal UI library (prompt_toolkit or Textual; must support the alternate screen and
  wrapping on Windows Terminal).
- Exact window-focus mechanism for live sessions.
- Whether the browser fork imports the core as a package or vendors it; either way one copy of
  the code.
- Test strategy against a fixture store (never the real `~\.claude`), mirroring the reorg
  tool's `fake_env` approach.

## Sequencing

The tree is still half migrated (stage 3 of the reorg moves `Client-Work`, `Workflows`,
`Rewst`, `halo`, `claude_improvements` and others). The launcher reads folders live, so it
can be built now against a fixture store that mirrors the **target** layout, but it ships
only after the stage-3 cutover, and the browser fork is referenced by whatever path it has
then (today `C:\claude-code\claude_improvements\claude-session-browser-main`; after stage 3
`cc\claude_improvements\...`).

## Open items (user)

1. ~~Credential route for the Haiku stage~~ settled: signed-in Claude Code session through
   `claude.exe -p`, run concurrently (see Non-blocking). User to confirm the concurrent shape.
2. ~~Repo creation~~ settled: auto-create, personal/work prompt defaulted to personal.
3. ~~Browser side~~ settled by inspection: one source copy (the fork, in sync with GitHub at
   915ae5d, two uncommitted files from another session); `ClaudeSessionBrowser` is the
   installed build. Decision taken here, user to confirm: the unified Claude+Qwen list
   **becomes the browser's main list** (Qwen rows carry a tool badge, filterable), "New
   session" is a toolbar action, and "done" includes a release cut with the existing
   `release.ps1` so the installed app actually has it.

## Browser integration detail

- The fork already has `sessions_live.py` (live session detection) and `termfocus.py`
  (bringing a terminal window forward): the launcher core **reuses** these instead of
  writing new ones; the builder moves or imports them, never duplicates.
- Main list gains a `tool` column and the Qwen store; existing per-row actions keep working
  for Claude rows; Qwen rows get resume and open-folder only until parity is wanted.
- "New session" opens the dialog in the round-3 mockup (columns walk, suggestion chip,
  wrapper chips, GitHub account chips) and launches in a new Windows Terminal tab.
