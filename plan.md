# Claude Session Browser (mhollier117 fork) — state

Repo: `mhollier117/claude-session-browser` (public; keep it public). Upstream: `juppeee/claude-session-browser`.
No GitHub Actions workflows in this repo: pushes cost no CI minutes.

## cc launcher (2026-11-05) - built, not released
- Status: built, reviewed and packaged on branch `launcher` (worktree `claude_improvements\csb-launcher`); not yet merged to `main`.
  Spec: `docs/superpowers/specs/2026-11-05-session-launcher-design.md`. Plan: `docs/superpowers/plans/2026-11-05-session-launcher.md`.
- What it is: `launcher/` package (unified Claude + Qwen index, resume list and new-session walk in `prompt_toolkit`,
  destination suggestion Qwen > Claude Haiku > history > keywords, scaffold with `plan.md`/`CLAUDE.client.md` templates),
  `cc_main.py` (PyInstaller entry), `tools/cc.ps1` (profile function), the browser's "New session" dialog and tool badge.
- Packaging: `build.ps1` now has 6 steps; step 4 builds `dist\cc.exe` (onefile, console). The main app build also bundles
  the two templates. `setup.iss` installs `cc.exe` and `cc.ps1` next to `ClaudeSessionBrowser.exe`. README has a "cc launcher" section.
  `*.spec` is gitignored and `ClaudeSessionBrowser.spec` is not used by `build.ps1`, so there is no `cc.spec`.
- Remains: (1) the user cuts the release after the controller merges `launcher` into `main` (fast-forward):
  `.elease.ps1 -Version 1.7.0.0 -Notes "cc launcher: unified Claude+Qwen list, New session, terminal cc" -Publish`;
  (2) Qwen prompt-size tuning (see `suggest.log`: latency and prompt size per attempt); (3) deferred minors, listed in the
  review ledger summary the controller adds at merge.

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
