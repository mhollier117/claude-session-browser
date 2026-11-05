"""Create a new folder in the tree: organisational/client/container folders, or a project with plan.md, a git repo,
an optional GitHub repo (personal mhollier117 or work mhollierBPT) and the `.claude-project` marker.

Every external command goes through a `Runner`, so tests can script git/gh without running them."""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from .tree import slug

_TEMPLATES = Path(__file__).resolve().parent / "templates"
WORK_TEMPLATE = Path.home() / ".claude" / "templates" / "work-github" / "CLAUDE.local.work-github.md"
PERSONAL_OWNER = "mhollier117"
WORK_USER = "mhollierBPT"
KINDS = ("organisational", "client", "container", "project")

# WORK-GITHUB.md step 3: repo-local credential helper (the token is fetched by git at use time, never stored),
# commit identity and the Windows OpenSSH client.
_WORK_HELPER = ('!f() { test "$1" = get || exit 0; echo username=x-access-token; '
                f'echo "password=$(gh auth token --user {WORK_USER})"; }}; f')
_WORK_CONFIG = (
    ["credential.https://github.com.helper", ""],
    ["--add", "credential.https://github.com.helper", _WORK_HELPER],
    ["user.name", "Michael Hollier"],
    ["user.email", "mhollier@bastionpoint.com"],
    ["core.sshCommand", "C:/Windows/System32/OpenSSH/ssh.exe"],
)


@dataclass
class Result:
    code: int
    out: str
    err: str


@dataclass
class Runner:
    """Runs a command without a shell. `env` holds extra variables layered over the current environment for this
    one call only."""

    def run(self, args: list[str], cwd: Path, env: dict | None = None, timeout: float = 60) -> Result:
        try:
            p = subprocess.run(args, cwd=cwd, env={**os.environ, **env} if env else None, capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=timeout)
        except FileNotFoundError as e:
            return Result(127, "", str(e))
        except subprocess.TimeoutExpired:
            return Result(124, "", f"timed out after {timeout}s: {args[0]}")
        return Result(p.returncode, p.stdout or "", p.stderr or "")


@dataclass
class Created:
    path: Path
    repo: str | None = None
    warnings: list[str] = field(default_factory=list)


class _Failed(Exception):
    pass


def _check_name(name: str) -> None:
    if not name or name in (".", "..") or name.startswith("."):
        raise ValueError(f"invalid folder name: {name!r}")
    if any(sep in name for sep in ("/", "\\", os.sep, os.altsep or "/")):
        raise ValueError(f"folder name must not contain a path separator: {name!r}")


def create_folder(parent: Path, name: str, kind: str, session_name: str, runner: Runner | None = None,
                  github: str | None = None) -> Created:
    """Make `parent/name` as `kind`. Validates before touching the disk; git/GitHub problems become warnings."""
    _check_name(name)
    if kind not in KINDS:
        raise ValueError(f"unknown folder kind: {kind!r}")
    if github not in (None, "personal", "work"):
        raise ValueError(f"unknown github account: {github!r}")
    runner = runner or Runner()
    path = Path(parent) / name
    path.mkdir()
    created = Created(path)
    if kind == "client":
        _write_template(path / "CLAUDE.md", "CLAUDE.client.md", name=name)
    elif kind == "project":
        _write_template(path / "plan.md", "plan.md", title=session_name)
        _scaffold_project(created, slug(name), runner, github)
    return created


def _write_template(dest: Path, template: str, **values: str) -> None:
    text = (_TEMPLATES / template).read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    dest.write_text(text, encoding="utf-8", newline="\n")


def _scaffold_project(created: Created, repo_name: str, runner: Runner, github: str | None) -> None:
    path = created.path

    def run(args: list[str], what: str, env: dict | None = None) -> str:
        r = runner.run(args, path, env=env)
        if r.code != 0:
            raise _Failed(f"{what} failed ({r.code}): {(r.err or r.out).strip()}")
        return r.out

    work_template = None
    if github == "work":
        try:
            work_template = WORK_TEMPLATE.read_text(encoding="utf-8")
        except OSError as e:
            created.warnings.append(f"work GitHub template unreadable ({WORK_TEMPLATE}): {e}; left a local repo only")
            github = None
    try:
        run(["git", "init"], "git init")
        if github == "work":  # before the first commit, so it carries the work identity
            for cfg in _WORK_CONFIG:
                run(["git", "config", "--local", *cfg], "git config")
        run(["git", "add", "-A"], "git add")
        run(["git", "commit", "-m", "chore: scaffold"], "git commit")
    except _Failed as e:
        created.warnings.append(f"{e}; GitHub repo not created")
        return
    if github is None:
        return
    try:
        if github == "personal":
            run(["gh", "repo", "create", repo_name, "--private", "--source", ".", "--push"], "gh repo create")
            repo = f"{PERSONAL_OWNER}/{repo_name}"
        else:
            repo = _create_work_repo(path, repo_name, run, work_template)
        sha = run(["git", "rev-parse", "HEAD"], "git rev-parse").strip()
    except _Failed as e:
        created.warnings.append(f"{e}; left a local repo only")
        return
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (path / ".claude-project").write_text(json.dumps({"repo": repo, "lastCommit": sha, "lastSynced": stamp}),
                                          encoding="utf-8")
    created.repo = repo
    if github == "work":
        created.warnings.extend(_work_local_files(path, repo, work_template))


def _work_token(run) -> dict:
    """WORK-GITHUB.md: the token is fetched per command and lives only in that command's environment. Failure
    means mhollierBPT isn't signed in to gh; signing in is interactive, so it's left to the user."""
    try:
        token = run(["gh", "auth", "token", "--user", WORK_USER], "gh auth token").strip()
    except _Failed as e:
        raise _Failed(f"{WORK_USER} is not signed in to gh (sign in per WORK-GITHUB.md step 1): {e}") from None
    if not token:
        raise _Failed(f"{WORK_USER} is not signed in to gh (sign in per WORK-GITHUB.md step 1)")
    return {"GH_TOKEN": token}


def _create_work_repo(path: Path, repo_name: str, run, template: str) -> str:
    """WORK-GITHUB.md step 2 ("doesn't exist yet") for a local repo that already has its first commit, then push
    through the repo-local credential helper configured from step 3."""
    run(["gh", "repo", "create", repo_name, "--private"], "gh repo create", env=_work_token(run))
    owner_name = run(["gh", "repo", "view", repo_name, "--json", "nameWithOwner", "-q", ".nameWithOwner"],
                     "gh repo view", env=_work_token(run)).strip()
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", owner_name):
        raise _Failed(f"gh repo view returned an unexpected name: {owner_name!r}")
    run(["git", "remote", "add", "origin", f"https://github.com/{owner_name}.git"], "git remote add")
    run(["git", "push", "-u", "origin", "HEAD"], "git push")
    return owner_name


def _work_local_files(path: Path, repo: str, template: str) -> list[str]:
    """WORK-GITHUB.md step 4: CLAUDE.local.md from the template, and both local files kept out of git."""
    owner, name = repo.split("/", 1)
    text, leftover = fill_work_template(template, owner, name, date.today().isoformat())
    (path / "CLAUDE.local.md").write_text(text, encoding="utf-8", newline="\n")
    exclude = path / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    lines = existing.splitlines()
    add = [f for f in ("CLAUDE.local.md", ".claude-project") if f not in lines]
    if add:
        sep = "" if not existing or existing.endswith("\n") else "\n"
        exclude.write_text(existing + sep + "".join(f + "\n" for f in add), encoding="utf-8", newline="\n")
    return [f"CLAUDE.local.md has unfilled placeholders: {', '.join(leftover)}"] if leftover else []


def fill_work_template(text: str, owner: str, name: str, today: str) -> tuple[str, list[str]]:
    """Fill the work CLAUDE.local.md template for a new, non-fork repo: drop the instructions comment and the
    upstream-only wording, substitute OWNER/NAME/DATE. Returns the text and any placeholders still left."""
    text = re.sub(r"<!--\s*Template:.*?-->\n*", "", text, flags=re.S)
    text = text.replace(", a fork of `{{UPSTREAM}}`", "")
    text = re.sub(r" For the upstream\s+repo, use `--repo \{\{UPSTREAM\}\}[^`]*`\.", "", text)
    for key, value in (("OWNER", owner), ("NAME", name), ("DATE", today)):
        text = text.replace("{{" + key + "}}", value)
    return text, re.findall(r"\{\{[A-Z_]+\}\}", text)
