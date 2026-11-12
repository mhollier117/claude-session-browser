<div align="center">

<img src="docs/logo.png" width="120" alt="Claude Session Browser">

# Claude Session Browser

[![Windows](https://img.shields.io/badge/Windows-10%20%7C%2011-0078d4)](https://github.com/juppeee/claude-session-browser/releases/latest)
[![Python](https://img.shields.io/badge/Python-3.11-3776ab)](https://www.python.org/)
[![UI](https://img.shields.io/badge/UI-pywebview%20%2B%20WebView2-ec7456)](https://pywebview.flowrl.com/)
[![License](https://img.shields.io/badge/License-MIT-3ecf8e)](LICENSE)
[![Release](https://img.shields.io/github/v/release/juppeee/claude-session-browser?color=ffb454)](https://github.com/juppeee/claude-session-browser/releases/latest)

**Every Claude Code session you ever started, in one window — search them, and double-click one to jump straight back in.**

https://github.com/user-attachments/assets/4718ca04-9353-42bd-baa3-5b24d559bd8d

<a href="https://github.com/juppeee/claude-session-browser/releases/latest/download/ClaudeSessionBrowser-Setup.exe"><img src="https://img.shields.io/badge/Download-Installer%20for%20Windows-ec7456?style=for-the-badge&logo=windows&logoColor=white" alt="Download the installer for Windows"></a>

<sub>Installs per user — no admin rights, no UAC prompt, and it never touches `~/.claude`</sub>

[Quick start](#quick-start) · [What you get](#what-you-get) · [Clawd](#clawd-your-desktop-buddy) · [Clawdmeter](#clawdmeter) · [Plugins](#plugins) · [Settings](#settings) · [Uninstall](#updating-and-uninstalling) · [Credits](#credits)

</div>

> [!NOTE]
> **This is a fork of [juppeee/claude-session-browser](https://github.com/juppeee/claude-session-browser)**, adapted to drive a Clawdmeter with several Claude accounts and several open Claude Code sessions. Everything below the "This fork" section describes the original app. The download badges above point at the original releases. This fork's builds come from its own releases (none published yet).

## This fork

Added on top of the original:

- **Accounts tab** (`accounts.py`): keep several Claude accounts and switch Claude Code between them without logging out. Also from the tray ("Switch account"). Shows 5-hour, weekly and **Fable** weekly usage per account, a weekly-only view for plans without a 5-hour limit, and re-login warnings. Sign-ins are stored DPAPI-encrypted in `%USERPROFILE%\.claude-accounts`, outside `.claude`.
- **Live session tracking** (`sessions_live.py`): reads Claude Code's own list of open sessions (`~/.claude/sessions`), so the desktop buddy and the Clawdmeter show the most urgent session (needs you › working › done › idle) and its name. No hooks, no cost to Claude.
- **Clawdmeter link, protocol 2** (`clawdmeter.py`): usage from the free usage endpoint (also for plans without a 5-hour limit), Fable usage, the session name, and lists of open sessions and accounts for firmware that supports them.
- **Faster start** with large histories (incremental, on-disk session index), English by default, an informational Fable-limit notification.
- **Scripts:** `build.ps1` (installer), `release.ps1` (version + checksum + GitHub release), `install.ps1` (checksum-verified install on any PC).

Development: Python 3.12 virtualenv in `.venv` (`uv venv .venv --python 3.12`, then `uv pip install -r requirements-dev.txt`). Tests: `.venv\Scripts\python.exe -m pytest -q tests`.


---

Claude Code keeps every session on disk under `~/.claude/projects`, but getting
back into one means digging out a session ID and typing `claude --resume`. This
app lists them all — title, folder, message count, when you last touched it —
and puts you back into one with a double-click.

<div align="center">

<img src="docs/screenshot-sessions.png" width="880" alt="The session list with the detail panel open">

<sub>Pick a session and everything about it is on the right — folder, message counts, and how it started</sub>

</div>

## What you get

- **Every session in one list** — Claude's auto-title or your own, folder, message count, last activity
- **Find it fast** — live search across title, folder, ID and first question; sortable columns you can show, hide and resize by dragging their borders
- **Make it yours** — colour-code sessions, rename them for good, copy the ID
- **One click back in** — opens Windows Terminal or `cmd` with the session resumed
- **Know where your quota stands** — 5-hour and weekly usage with a live countdown to the reset
- **Get told, not surprised** — a heads-up before the limit is full, and a notification when it resets
- **[Clawd](#clawd-your-desktop-buddy)** — a 20×20 pixel buddy on your desktop who acts out what Claude is doing
- **[Clawdmeter](#clawdmeter) support** — mirror Clawd onto a real device over Bluetooth
- **German and English**, following your Windows language
- **Updates itself** from GitHub

## Quick start

**[⬇ Download ClaudeSessionBrowser-Setup.exe](https://github.com/juppeee/claude-session-browser/releases/latest/download/ClaudeSessionBrowser-Setup.exe)** and run it. That's the whole installation.

It installs per user, so **no admin rights and no UAC prompt**, and it never
touches `~/.claude` — your sessions and settings are none of the installer's
business. The app starts by itself when the installer finishes.

> **First launch:** Windows may show a SmartScreen warning ("unknown publisher")
> because the app isn't code-signed. Click **More info → Run anyway**. It won't
> ask again — the installed copy carries no "downloaded from the web" mark.

<details>
<summary><b>Run it from source instead</b></summary>

```bash
git clone https://github.com/juppeee/claude-session-browser.git
cd claude-session-browser
pip install pywebview pystray Pillow
python claude_sessions.py
```

`pywebview` renders through the Edge WebView2 engine, which ships with Windows
10 and 11. Bluetooth for the Clawdmeter needs one more package:

```bash
pip install bleak
```

</details>

<details>
<summary><b>Build your own installer</b></summary>

```bash
pip install pyinstaller
winget install JRSoftware.InnoSetup
build.bat
```

Three files land in `dist\`: the installer, a standalone one-file exe, and the
separate updater.

</details>

## Linux and macOS

The Session Browser is built and tested on Windows only. Others have ported it:

- **Linux** — [JayceTheGaymer/claude-session-browser](https://github.com/JayceTheGaymer/claude-session-browser) by @JayceTheGaymer ("Tested on CachyOS with KDE Plasma only")
- **macOS** — [ryanmaule/claude-session-browser](https://github.com/ryanmaule/claude-session-browser/tree/macos-ble-port) by @ryanmaule
- **macOS** — [amcgready/claude-session-browser](https://github.com/amcgready/claude-session-browser) by @amcgready
- **macOS** — [wdonega/claude-session-browser](https://github.com/wdonega/claude-session-browser) by @wdonega

These are independent forks. We haven't tested them — questions and bugs belong in the fork's own issues.

## Clawd, your desktop buddy

Clawd is a tiny animated character who sits on your desktop and shows what
Claude Code is up to — thinking, writing code, waiting for permission, out of
quota. Fifteen animations, chosen from what is actually happening in your
sessions rather than from a timer.

<div align="center">

<img src="docs/clawd-idle.png" height="120" alt="Clawd idle"> <img src="docs/clawd-thinking.png" height="120" alt="Clawd thinking"> <img src="docs/clawd-coding.png" height="120" alt="Clawd writing code"> <img src="docs/clawd-limit.png" height="120" alt="Clawd out of quota">

<sub>Waiting · thinking · writing code · out of quota</sub>

</div>

He comes with a frame or without. Turn the frame off and the backdrop goes with
it — what's left is just Clawd, floating on your desktop:

<div align="center">

<img src="docs/clawd-sleeping.png" height="100" alt="Clawd asleep, no frame"> <img src="docs/clawd-desk.png" height="100" alt="Clawd at his desk, no frame">

</div>

Switch him on in the **Buddy** tab. He can be there all the time or only while
Claude Code is running. Drag him wherever you like; right-click sends him away
for a while, and he returns with your next Claude Code terminal.

**Spotting permission prompts reliably.** Claude Code writes its question to the
terminal, never to the transcript, so the app has to infer it — and inference
goes wrong as soon as several terminals are open: one is working, another is
asking, and the window title belongs to the window, not the tab.

Turn on *Spot permission prompts reliably* under **Settings → Connections** and
Claude Code reports it itself. That adds a single hook to
`~/.claude/settings.json`; your other hooks stay untouched, and it applies to
sessions you start afterwards.

## Clawdmeter

*Optional, and someone else's project — skip this if you don't own the device.*

The [Clawdmeter](https://github.com/HermannBjorgvin/Clawdmeter) is a small ESP32
device by [Hermann Björgvin](https://github.com/HermannBjorgvin) that displays
your Claude usage. This app speaks to it over Bluetooth on Windows and can
mirror Clawd onto it, so the device acts out the same state your desktop buddy
does, rather than only reacting to how fast your quota is burning. It reports
its battery level back, and warns you before it runs flat.

<div align="center">

<img src="docs/clawdmeter-case.png" width="330" alt="Clawdmeter in a printed case, showing usage and the buddy"> <img src="docs/clawdmeter-desk.png" width="330" alt="Clawdmeter on a desk next to a keyboard">

<sub>Renders of the printable case — usage on one screen, Clawd on the other</sub>

</div>

Pair the device once in the Windows Bluetooth settings, then enable it in the
**Clawdmeter** tab.

**Stock firmware is enough for most of it.** Usage and battery need nothing
special — flash Hermann's firmware as usual and the device shows your quota
while picking its own animations, exactly as it does without this app.

Mirroring Clawd onto it needs two things his firmware doesn't carry yet: a
field in the BLE payload so a host can name an animation, and sprites for the
states it doesn't know — waiting for permission, out of quota. Until those land
upstream they live in a [fork of his firmware](https://github.com/juppeee/Clawdmeter/tree/csb-buddy);
flash that branch and the device acts out what Claude is doing.

**Want a case for it?** The STL files are on
[MakerWorld](https://makerworld.com/de/@Juppi187) — print one and your
Clawdmeter gets a body to match the buddy.

See [Credits](#credits) for who built what.

## Plugins

The **Store** tab lists small add-on tools. Installing one adds a tab of its own;
the switch next to it turns it off without removing it. Plugins come only from
the reviewed catalogue in this repository, and every version is checked against
its SHA-256 before it is unpacked.

Most plugins are HTML and JavaScript and reach data only through the app, with no
access to your files or your Claude token. A plugin that needs more says so in
its manifest, and the store shows what it asks for. Starting the app with
**Shift** held down loads no plugins at all.

Writing one is described in [docs/plugins.md](docs/plugins.md); `plugins/ssd-health/`
is a complete example.

## cc launcher

`cc` resumes or starts a Claude Code or Qwen session in the terminal tab you are
already in: one list of every Claude and Qwen session, and a short walk for a new one.
The installer puts `cc.ps1` next to `ClaudeSessionBrowser.exe` and the launcher itself in
the `cc` folder beside it (`cc\cc.exe`). Add one line to your PowerShell profile
(`notepad $PROFILE`):

```powershell
. "$env:LOCALAPPDATA\Programs\ClaudeSessionBrowser\cc.ps1"
```

That is the default install folder; if you installed elsewhere, use `. "<app dir>\cc.ps1"`.
Then type `cc`. The function changes to the session's folder and runs the wrapper in
that tab; `cc.ps1` runs `cc\cc.exe` beside it, else a `cc.exe` beside it, else the
launcher from the repository's `.venv`, else `python -m launcher.cli`.

- `cc`: the resume list. `cc new [name]`: start a new session. `cc index`: refresh the
  index only. `cc --json`: print every session as JSON.
- **List keys:** ↑↓ move · type to filter · tab preview · enter resume · ctrl-n new ·
  ctrl-o open folder · ctrl-w change wrapper · ctrl-q (or ctrl-c) quit. Every printable key,
  letters and spaces included, goes to the filter; space opens the preview only while the
  filter is empty. Esc clears the filter. The list is ordered by the time of each session's
  last record, not by when its file was last touched.
- **New session walk:** name, then a destination picker, then one confirm step that
  shows the folder, wrapper, GitHub choice and the exact command. enter confirms or
  chooses, esc goes back (in the picker: to the root), ctrl-c quits. In the picker every
  letter, `q` included, filters the folders, and typed text that matches no folder becomes
  the new folder's name. On the confirm step ctrl-w changes the wrapper and ctrl-g the
  GitHub choice (personal, work or none); the choices are remembered per bucket in
  `defaults.json`.

**How the destination suggestion works.** Local retrieval ranks the folders at once (the
picker never waits) and supplies the alternatives. In the background the self-hosted Qwen
endpoint is asked first (only reachable on the VPN, 6 seconds in total); Claude Code
(Haiku), using the Claude session you are already signed in with (20 seconds), is asked
only if Qwen was skipped or failed. The model answer is offered as a suggestion; it never
moves the picker. Every attempt, and the folder you finally chose, is appended to
`%LOCALAPPDATA%\cc-launcher\suggest.log` (stage, name, latency, outcome and prompt size;
never the API key); each step of the walk goes to `flow.log` beside it.

The first run after this update rebuilds the index, which takes a few minutes; a second
`cc` or the app may report "index busy" meanwhile.

**Where things live, and overrides.** The index is `%LOCALAPPDATA%\cc-launcher\index.sqlite`.
Each location can be overridden with an environment variable:

| Variable | Default | What it is |
|---|---|---|
| `CC_LAUNCHER_ROOT` | `C:\claude-code` | project tree the picker browses |
| `CC_LAUNCHER_DATA` | `%LOCALAPPDATA%\cc-launcher` | index, `suggest.log`, `flow.log`, `defaults.json`, remembered wrappers |
| `CC_LAUNCHER_QWEN_HOME` | `~\.qwen` | Qwen's session store |
| `CLAUDE_CONFIG_DIR` | `~\.claude` | Claude's session store |

The session browser's **New session** dialog uses the same launcher.

## Settings

1. Open the **Settings** tab
2. Point **Sessions folder** at your Claude projects directory — it is found automatically, but you can override it
3. Pick colours, columns and language under **Appearance**
4. Choose how sessions open under **Connections**

| Setting | Default | What it does |
|---|---|---|
| Language | Automatic | German on German Windows, English everywhere else |
| Open with | Automatic | Windows Terminal, or `cmd` if that's missing |
| Claude command | `claude` | Path or name of the Claude CLI |
| Keep running in background | On | The X button hides the app in the system tray |
| Start with Windows | On | Registry entry under `HKCU\Run` |
| Notify on limit reset | On | A Windows notification when your quota is back |
| Warn before the limit is full | On, at 90% | Once per 5-hour window |
| Clawdmeter battery warning | On, at 15% | Once per discharge |

<details>
<summary><b>Where your data lives</b></summary>

| Path | Contents |
|---|---|
| `~/.claude/projects/` | Your Claude Code sessions — read only, never modified |
| `~/.claude/session_browser_settings.json` | This app's settings |
| `~/.claude/session_titles.json` | Titles you renamed yourself |
| `~/.claude/settings.json` | Claude Code's own settings — only touched if you enable the hook |
| `~/.claude/csb_hooks/` | What the hook reports, one small file per session |

</details>

<details>
<summary><b>How the state detection works</b></summary>

The app tails the newest session transcript and works out what Claude is doing
from the last few entries: a `thinking` block, a text response, a tool call with
no result yet, an `end_turn`, or an API error carrying a rate-limit status. That
state decides which animation Clawd plays and what the Clawdmeter shows.

Two things are deliberately not read from the transcript, because they aren't in
it: the permission prompt (see [above](#clawd-your-desktop-buddy)), and your
exact quota, which comes from the API rate-limit headers instead.

</details>

<details>
<summary><b>Under the hood</b></summary>

Clawd's animations are 20×20 pixel sprites with a 10-colour palette, packed into
`clawd_sprites.py` by `pack_sprites.py`.

Interface text lives in `i18n.py`, where the German sentence is the key — a
missing translation shows German rather than an empty label.
`tools/check_i18n.py` verifies every string has an English version and that
placeholders match on both sides; it runs as the first step of every build.

</details>

<details>
<summary><b>Publishing a release</b> (maintainer)</summary>

1. Raise `VERSION` in `claude_sessions.py`
2. Run `build.bat` and attach the installer and the one-file exe to a GitHub release
3. Update `version.json` with the same version and a short note, then push

The app compares its own `VERSION` against `version.json` in this repo on start.

</details>

## Updating and uninstalling

The app checks GitHub for updates by itself and offers to install them. No
internet, no problem — the check is skipped silently.

To remove it: **Settings → Apps → Claude Session Browser → Uninstall**. Your
sessions, titles and settings under `~/.claude` survive. Delete
`session_browser_settings.json` and `session_titles.json` by hand if you want
those gone too.

## Credits

**The Clawdmeter is not this project's work.** The device and its firmware are the work of [Hermann Björgvin](https://github.com/HermannBjorgvin/Clawdmeter) — the hardware abstraction, five board ports, the LVGL interface, the BLE service and the animation engine are all his.

Talking to it is one feature of this app among many. The Session Browser is first and foremost a browser for your Claude Code sessions: it searches them, colour-codes them and puts you back into one with a double-click, and it does all of that without a Clawdmeter anywhere in sight.

**Clawd himself** comes from [claudepix](https://claudepix.vercel.app) by [@amaanbuilds](https://x.com/amaanbuilds), a library of pixel-art Clawd sprites — the same source Hermann's firmware draws on. Some of the animations here were taken from there, others inspired by it, and nearly all have been reworked or redrawn since. Go and have a look, it's where Clawd got his face.

What this project adds on top of Hermann's work is two things: the Bluetooth connection for Windows (his daemon is a Linux shell script built on bluez), and **activity-driven animations**. Upstream picks an animation from how fast your quota is burning — a rate measured over a six-sample ring buffer and grouped into calm / normal / active / heavy. It cannot know *what* Claude is doing. The Session Browser reads the session transcripts, works out the actual state — thinking, writing code, waiting for permission, out of quota — and tells the device which animation to show. Turn that off and the device falls back to Hermann's usage groups.

## License

The **code** in this repository is MIT — see [LICENSE](LICENSE).

That covers the code and nothing else. Clawd is Anthropic's mascot, and the
sprites in `clawd_sprites.py` are him; they started at
[claudepix](https://claudepix.vercel.app) and were reworked from there. The
artwork isn't mine to license, so the MIT grant doesn't reach it. The Clawdmeter
repository carries the same note.
