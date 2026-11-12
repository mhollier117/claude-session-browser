# Claude Session Browser (mhollier117 fork) — state

Repo: `mhollier117/claude-session-browser` (public; keep it public). Upstream: `juppeee/claude-session-browser`.
No GitHub Actions workflows in this repo: pushes cost no CI minutes.

## Crash fix + release 1.7.0.0 (2026-11-12)
- The app vanished without a message (7 APPCRASH events since 11-06, tcl86t `Tcl_PanicVA`, 0x80000003):
  the buddy's and the limit-reset card's Tk roots stayed in reference cycles after closing (self-calling
  `slide`/`fade`/`tick` closures) and were freed by the GC on another thread -> "Tcl_AsyncDelete: async handler
  deleted by the wrong thread". Fix: `_run_tk_thread` runs each Tk thread body and `gc.collect()`s on that thread.
  Test: `tests/test_tk_threads.py` (child process; crashes with 0x80000003 without the helper).
- Released as 1.7.0.0 together with the launcher (launcher fast-forwarded into main).
- Known, not fixed: the single-instance guard starts a second copy after 1.5 s if the first has no window yet.

## cc launcher round 2 (2026-11-12) - implemented and reviewed, released in 1.7.0.0
- Plan: `docs/superpowers/plans/2026-11-11-launcher-round2.md` (five tasks, all done). Commits `e58bbf6..2fc0414` on
  `launcher` (base `248da48`). Full suite on `2fc0414`: 718 passed. Ledger with rulings and parked items:
  `.superpowers\round2\ledger.md` (git-ignored).
- What changed:
  - Suggester v2: local retrieval (`launcher/retrieve.py`) builds a short numbered list, the model picks a number.
    Qwen first, Claude Haiku only if Qwen was skipped or failed. Live eval (`tools\suggest_eval.py`): 29/29 valid,
    21/24 held-out, 26/29 overall, about 1 s. The eval needs a current index: run `cc` once first.
  - Picker: typed text is the new folder's name (`+ new folder: <slug>`), a suggestion is only ever a row, selection
    is kept by identity, the footer shows the exits.
  - Resume list: ordered by the last record's timestamp, typing always filters, ctrl chords for actions. Index schema 2
    (the first run rebuilds the index, a few minutes; a second `cc` or the app may say "index busy" meanwhile).
  - One confirm step with remembered per-bucket defaults (`launcher/defaults.py`, `defaults.json`), GitHub `none`.
    Confirm keys are ctrl-w (wrapper) and ctrl-g (GitHub); typing on a start-here confirm goes back to the picker as
    the new folder's name; typing on a new-project confirm only shows a hint.
  - Every launched command clears `CLAUDE_CODE_CHILD_SESSION` and sets `CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1`
    (`tools/cc.ps1`, `bridge.CHILD_SESSION_PREFIX`).
  - Browser dialog: suggestion once per finished name (Enter or blur), chips with up to two alternatives, typed folder
    name wins, bucket defaults, flow log (`flow.log`, `ui: browser|terminal`) and outcome lines in `suggest.log`.
- Remains: (1) `origin/launcher` still holds the pre-rebase branch (PR #1 shows CONFLICTING); updating it needs a force
  push, which needs the user's OK. (2) The user tests the build, then decides on merge and release (below).
  (3) Not done, the user's call: the browser's Claude resume path (`claude_sessions.py` ~1356, upstream) sets
  `CLAUDE_CODE_FORCE_SESSION_PERSIST`, a name Claude Code does not read, and does not clear the child marker.
  (4) Parked minors are in the ledger (versioned index file name, hint not cleared by ctrl-w/ctrl-g, `_dec2024`-style
  suffixes treated as hashes, no JS test harness, no `cc.ps1` test).

## cc launcher (2026-11-05) - built, not released
- Status: built, reviewed and packaged on branch `launcher` (worktree `claude_improvements\csb-launcher`); not yet merged to `main`.
  Spec: `docs/superpowers/specs/2026-11-05-session-launcher-design.md`. Plan: `docs/superpowers/plans/2026-11-05-session-launcher.md`.
- What it is: `launcher/` package (unified Claude + Qwen index, resume list and new-session walk in `prompt_toolkit`,
  destination suggestion (see round 2 above), scaffold with `plan.md`/`CLAUDE.client.md` templates),
  `cc_main.py` (PyInstaller entry), `tools/cc.ps1` (profile function), the browser's "New session" dialog and tool badge.
- Packaging: `build.ps1` now has 6 steps; step 4 builds `dist\cc\cc.exe` + `_internal` (onedir, console; `--help` ~0.5 s
  warm). The main app build also bundles the two templates. `setup.iss` installs `dist\cc\` as `{app}\cc\` and `cc.ps1`
  next to `ClaudeSessionBrowser.exe`. README has a "cc launcher" section.
  `*.spec` is gitignored and `ClaudeSessionBrowser.spec` is not used by `build.ps1`, so there is no `cc.spec`.
- Final-review fix wave done (report: `.superpowers/sdd/2026-11-05-session-launcher/final-fix-report.md`): level rule
  (root and `work` organisational, `work\clients` client, everything else project/container), WAL + 30 s busy timeout
  (cc exits 2 "index busy"), cc onedir,
  `q` filters in the picker, 16 MB tail cap, `;` rejected in folder names.
- Remains: (1) the user cuts the release after the controller merges `launcher` into `main` (fast-forward):
  `.\release.ps1 -Version 1.7.0.0 -Notes "cc launcher: unified Claude+Qwen list, New session, terminal cc" -Publish`;
  (2) deferred minors, listed in the review ledger summary the controller adds at merge.

## Current state (2026-11-06)
- Latest release: **1.6.1.10** (installer, install-here zip, portable zip). Work happens on two PCs;
  1.6.1.8 and 1.6.1.9 came from the other one. Always `git fetch` and fast-forward before releasing.
- Account store folders on Windows inherit the profile ACL (`accounts._private_mkdir`); together with
  1.6.1.9's `runasoriginaluser` installer change this closes the "PermissionError ... signin.bin" bug.
- Build environment on this PC: `.venv` (Python 3.12, per-user) + Inno Setup 6 (per-user, winget).
  Release: `.\release.ps1 -Version <x.y.z.n> -Notes "..." -Publish` (runs i18n check, full tests, builds, commits, pushes, publishes).

## Done this session
- Investigated "Transcript writes are failing (EBUSY)". **Not caused by this app.** Root cause was the user's
  status line `~/.claude/scripts/context-bar.ps1` (parsed the whole transcript with `[IO.File]::ReadLines`,
  FileShare.Read → blocks Claude's appends; transcripts up to 2 GB). Fixed there (uses stdin
  `context_window.current_usage`); the "claude statusline" session ported it to its source
  (`claude-code-tips-main\claude-code-tips-win\scripts`) and redeployed. The app's own transcript reads were
  verified harmless (Python open() shares read+write).
- Fixed `hooks/csb-name-gate.ps1`: messages from other Claude sessions (`<cross-session-message`) are no longer
  blocked in unnamed sessions (commit a938077, released in 1.6.1.7).

## Next / open
- Nothing pending. Optional status line perf ideas (single `git status --porcelain=v2`, drop second pwsh)
  belong to the claude-code-tips-main project, not here.
