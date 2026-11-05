# Claude Session Browser (mhollier117 fork) — state

Repo: `mhollier117/claude-session-browser` (public; keep it public). Upstream: `juppeee/claude-session-browser`.
No GitHub Actions workflows in this repo: pushes cost no CI minutes.

## cc launcher (2026-11-05) - built, not released
- Status: built, reviewed and packaged on branch `launcher` (worktree `claude_improvements\csb-launcher`); not yet merged to `main`.
  Spec: `docs/superpowers/specs/2026-11-05-session-launcher-design.md`. Plan: `docs/superpowers/plans/2026-11-05-session-launcher.md`.
- What it is: `launcher/` package (unified Claude + Qwen index, resume list and new-session walk in `prompt_toolkit`,
  destination suggestion Qwen > Claude Haiku > history > keywords, scaffold with `plan.md`/`CLAUDE.client.md` templates),
  `cc_main.py` (PyInstaller entry), `tools/cc.ps1` (profile function), the browser's "New session" dialog and tool badge.
- Packaging: `build.ps1` now has 6 steps; step 4 builds `dist\cc\cc.exe` + `_internal` (onedir, console; `--help` ~0.5 s
  warm). The main app build also bundles the two templates. `setup.iss` installs `dist\cc\` as `{app}\cc\` and `cc.ps1`
  next to `ClaudeSessionBrowser.exe`. README has a "cc launcher" section.
  `*.spec` is gitignored and `ClaudeSessionBrowser.spec` is not used by `build.ps1`, so there is no `cc.spec`.
- Final-review fix wave done (report: `.superpowers/sdd/2026-11-05-session-launcher/final-fix-report.md`): level rule
  (root and `work` organisational, `work\clients` client, everything else project/container), WAL + 30 s busy timeout
  (cc exits 2 "index busy"), browser model stages only after 1.5 s idle (`Pending.cancel`, `defer_models`), cc onedir,
  `q` filters in the picker, 16 MB tail cap, `;` rejected in folder names.
- Remains: (1) the user cuts the release after the controller merges `launcher` into `main` (fast-forward):
  `.\release.ps1 -Version 1.7.0.0 -Notes "cc launcher: unified Claude+Qwen list, New session, terminal cc" -Publish`;
  (2) Qwen prompt-size tuning (see `suggest.log`: latency and prompt size per attempt); (3) deferred minors, listed in the
  review ledger summary the controller adds at merge.

## Current state (2026-11-03)
- Latest release: **1.6.1.7** (installer, install-here zip, portable zip). Installed on this PC via the
  install-here zip at `C:\claude-code\ClaudeSessionBrowser` (Install.cmd next to it in `C:\claude-code`).
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
