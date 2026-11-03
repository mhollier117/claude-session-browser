# Claude Session Browser (mhollier117 fork) — state

Repo: `mhollier117/claude-session-browser` (public; keep it public). Upstream: `juppeee/claude-session-browser`.
No GitHub Actions workflows in this repo: pushes cost no CI minutes.

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
