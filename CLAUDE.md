# Claude Session Browser: working notes

## Releasing
- Release from the main checkout: `.\release.ps1 -Version x.y.z.n -Notes "..." -Publish`. It ships whatever `main` has: fast-forward first (`git merge --ff-only launcher`) and confirm the change is on main (1.7.0.5 shipped without its change because the merge failed and the release ran anyway).
- Without `-Publish`, release.ps1 leaves a local "Release x" commit whose version.json announces the new version. The updater reads version.json from `main` (raw GitHub), so do not push `main` until the release is published: pushing a later commit takes that version.json along and installed copies are offered an installer that 404s (happened with 1.7.0.10, reverted).
- release.ps1 prints "Published" even when `gh release create` failed: check with `gh release list --repo mhollier117/claude-session-browser --limit 1`.
- Push and publish need gh's active account to be `mhollier117`; with `mhollierBPT` active the push gets 403. Never `gh auth switch`: for one command set `$env:GH_TOKEN = gh auth token --user mhollier117`.

## Tests
- Run with `$env:TEMP = $env:TMP = "$env:LOCALAPPDATA\Temp"`: if TEMP is under the app folder, `test_store_folders_inherit_profile_acl` fails.
- `tests/test_name_gate.py` has a flaky 30 s subprocess timeout under load: rerun that file alone.
- `tools\check_i18n.py`: a `t("...")` text must be one string literal (no implicit concatenation); German source, English in `i18n.py` `TRANSLATIONS["en"]`.

## Code gotchas
- Tk windows run on worker threads: start them through `_run_tk_thread` (a GC on another thread aborts the app with Tcl_AsyncDelete).
- Launch Claude only through `launch_in_terminal` (pwsh + profile, `-EncodedCommand`, child-session marker cleared), never cmd.exe: the wrappers (`clauded`, `claudez`, ...) are PowerShell profile functions.
- Never scan live transcripts with PowerShell `Select-String`/`Get-Content` (it can block Claude Code's writes); use Python `open()` on the file tail.

## Diagnosing a crash or freeze
- Crash dumps: `%LOCALAPPDATA%\CrashDumps\ClaudeSessionBrowser.exe.*.dmp`; Application log event 1000.
- Stacks of a live, frozen copy: `cdbX64 -pv -p <pid> -c "~*kn 40; qd"` (non-invasive). Starts are logged in `%LOCALAPPDATA%\ClaudeSessionBrowser\startup.log`.
