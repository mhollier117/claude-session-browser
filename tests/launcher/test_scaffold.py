import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from launcher import scaffold
from launcher.scaffold import Result, create_folder

_TOKEN = "gho_SECRETtoken0123456789"
_SHA = "0123456789abcdef0123456789abcdef01234567"


class FakeRunner:
    """Records every command and answers from a script keyed by command prefix (longest match wins);
    unscripted commands succeed with no output. Never runs anything."""

    def __init__(self, script: dict[str, tuple[int, str, str]] | None = None):
        self.script = script or {}
        self.calls: list[list[str]] = []
        self.envs: list[dict | None] = []
        self.cwds: list[Path] = []

    def run(self, args, cwd, env=None, timeout=60):
        self.calls.append(list(args))
        self.envs.append(env)
        self.cwds.append(Path(cwd))
        line = " ".join(args)
        keys = sorted((k for k in self.script if line.startswith(k)), key=len, reverse=True)
        return Result(*self.script[keys[0]]) if keys else Result(0, "", "")


def _files_under(root: Path):
    return [p for p in root.rglob("*") if p.is_file()]


def test_project_scaffold_personal(fake_env):
    runner = FakeRunner({"gh repo create": (0, "", ""), "git rev-parse HEAD": (0, "abc123\n", "")})
    c = create_folder(fake_env.root / "work" / "net", "fortinet_api_v2", "project", "FortiOS 7.6 auth", runner,
                      "personal")
    assert c.path == fake_env.root / "work" / "net" / "fortinet_api_v2"
    assert (c.path / "plan.md").read_text().startswith("# FortiOS 7.6 auth")
    assert ["gh", "repo", "create", "fortinet_api_v2", "--private", "--source", ".", "--push"] in runner.calls
    marker = json.loads((c.path / ".claude-project").read_text())
    assert marker["lastCommit"] == "abc123"
    assert marker["repo"] == "mhollier117/fortinet_api_v2" == c.repo
    assert set(marker) == {"repo", "lastCommit", "lastSynced"}
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", marker["lastSynced"])
    assert c.warnings == []
    assert all(cwd == c.path for cwd in runner.cwds)


def test_project_scaffold_runs_git_in_order(fake_env):
    runner = FakeRunner({"git rev-parse HEAD": (0, _SHA + "\n", "")})
    c = create_folder(fake_env.root / "cc", "tool", "project", "Tool", runner, "personal")
    assert runner.calls[:3] == [["git", "init", "-b", "main"], ["git", "add", "-A"], ["git", "commit", "-m", "chore: scaffold"]]
    assert runner.calls.index(["gh", "repo", "create", "tool", "--private", "--source", ".", "--push"]) == 3
    assert json.loads((c.path / ".claude-project").read_text())["lastCommit"] == _SHA


def test_plan_md_exact_content(fake_env):
    c = create_folder(fake_env.root / "cc", "x", "project", "My Title", FakeRunner(), None)
    assert (c.path / "plan.md").read_text(encoding="utf-8") == (
        "# My Title\n\n## Goal\n\nMy Title\n\n## Current phase\n\nNot started.\n\n## Next\n\n- \n\n"
        "## Decisions\n\n## Known issues\n\nNone.\n")


def test_project_without_github_makes_local_repo_only(fake_env):
    runner = FakeRunner()
    c = create_folder(fake_env.root / "cc", "local_only", "project", "Local", runner, None)
    assert [a[0] for a in runner.calls] == ["git", "git", "git"]
    assert not (c.path / ".claude-project").exists()
    assert c.repo is None and c.warnings == []


def test_project_scaffold_gh_failure_leaves_git_only(fake_env):
    runner = FakeRunner({"gh repo create": (1, "", "HTTP 422: name already exists")})
    c = create_folder(fake_env.root / "work" / "net", "dup", "project", "Dup", runner, "personal")
    assert not (c.path / ".claude-project").exists()
    assert c.repo is None
    assert len(c.warnings) == 1 and "name already exists" in c.warnings[0]
    assert (c.path / "plan.md").exists()


def test_git_commit_failure_skips_github(fake_env):
    runner = FakeRunner({"git commit": (128, "", "Please tell me who you are")})
    c = create_folder(fake_env.root / "cc", "noid", "project", "No id", runner, "personal")
    assert not any(a[0] == "gh" for a in runner.calls)
    assert len(c.warnings) == 1 and "who you are" in c.warnings[0]
    assert not (c.path / ".claude-project").exists()


def test_client_folder_gets_claude_md(fake_env):
    runner = FakeRunner()
    c = create_folder(fake_env.root / "work" / "clients", "acme", "client", "ignored", runner, "personal")
    assert c.path.is_dir()
    assert (c.path / "CLAUDE.md").read_text(encoding="utf-8") == "# acme\n\nClient work for acme.\n"
    assert sorted(p.name for p in c.path.iterdir()) == ["CLAUDE.md"]
    assert runner.calls == [] and c.repo is None and c.warnings == []


@pytest.mark.parametrize("kind", ["organisational", "container"])
def test_organisational_and_container_make_folder_only(fake_env, kind):
    runner = FakeRunner()
    c = create_folder(fake_env.root / "work", "stuff", kind, "ignored", runner, "personal")
    assert c.path.is_dir() and list(c.path.iterdir()) == []
    assert runner.calls == [] and c.repo is None and c.warnings == []


@pytest.mark.parametrize("name", ["", ".", "..", ".hidden", "a/b", "a\\b", "../escape", "D:evil", "C:evil",
                                  "a<b", "a>b", 'a"b', "a|b", "a?b", "a*b", "a;b", "x; calc"])
def test_bad_names_rejected_before_touching_disk(fake_env, name):
    parent = fake_env.root / "cc"
    before = sorted(p.name for p in parent.iterdir())
    runner = FakeRunner()
    with pytest.raises(ValueError):
        create_folder(parent, name, "project", "T", runner, "personal")
    assert sorted(p.name for p in parent.iterdir()) == before
    assert sorted(p.name for p in parent.parent.iterdir()) == ["cc", "home", "work"]
    assert runner.calls == []


def test_semicolon_rejected_everywhere_a_folder_name_is_checked(fake_env):
    from launcher import bridge
    from launcher.index import Index
    from launcher.scaffold import check_name
    from launcher.suggest import validate
    from launcher.tui.new import NewFlow
    with pytest.raises(ValueError, match=";"):
        check_name("a;b")
    assert validate(fake_env, {"path": "home", "new_folder": "a;b"}) is None  # the model's new_folder
    with pytest.raises(ValueError, match=";"):  # the browser's New session request
        bridge.parse_new(fake_env.root, {"name": "x", "rel": "home", "new_folder": "a;b", "kind": "project",
                                         "wrapper": "clauded"})
    idx = Index(fake_env, claude_live=lambda: [], pid_exists=lambda pid: False)
    try:
        flow = NewFlow(fake_env, idx, FakeRunner(), http=lambda *a: (_ for _ in ()).throw(OSError("offline")))
        flow.start("x")
        flow.esc()
        flow.selected = next(i for i, (k, _l, p) in enumerate(flow.rows) if k == "child" and p.name == "home")
        flow.enter()
        flow.selected = [k for k, _l, _p in flow.rows].index("new")
        flow.enter()
        flow.submit_folder_name("a;b")  # the terminal walk shows the error inline and stays on the name
        assert flow.step == "folder_name" and ";" in flow.error
    finally:
        idx.close()
    assert not (fake_env.root / "home" / "a;b").exists()


def test_unknown_kind_rejected(fake_env):
    with pytest.raises(ValueError):
        create_folder(fake_env.root / "cc", "x", "bogus", "T", FakeRunner(), None)
    assert not (fake_env.root / "cc" / "x").exists()


# --- work account -----------------------------------------------------------------------------------------------

def _work_runner(**overrides):
    script = {
        "gh auth token --user mhollierBPT": (0, _TOKEN + "\n", ""),
        "gh repo create": (0, "https://github.com/mhollierBPT/wproj\n", ""),
        "gh repo view": (0, "mhollierBPT/wproj\n", ""),
        "git rev-parse HEAD": (0, _SHA + "\n", ""),
    }
    script.update(overrides)
    return FakeRunner(script)


@pytest.fixture
def work_template(tmp_path, monkeypatch):
    real = Path.home() / ".claude" / "templates" / "work-github" / "CLAUDE.local.work-github.md"
    text = real.read_text(encoding="utf-8") if real.is_file() else (
        "# CLAUDE.local.md\n\n<!-- Template: x\n     Fill in. -->\n\n"
        "This repo (`{{OWNER}}/{{NAME}}`, a fork of `{{UPSTREAM}}`) set up on {{DATE}}.\n")
    path = tmp_path / "tpl" / "CLAUDE.local.work-github.md"
    path.parent.mkdir()
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(scaffold, "WORK_TEMPLATE", path)
    return path


def test_work_account_sequence(fake_env, work_template):
    runner = _work_runner()
    c = create_folder(fake_env.root / "work" / "net", "wproj", "project", "W", runner, "work")
    calls = [" ".join(a) for a in runner.calls]
    assert calls[0] == "git init -b main"
    i_cfg = calls.index("git config --local user.email mhollier@bastionpoint.com")
    assert i_cfg < calls.index("git commit -m chore: scaffold")
    assert "git config --local credential.https://github.com.helper " in calls
    assert any(a[:4] == ["git", "config", "--local", "--add"] and "gh auth token --user mhollierBPT" in a[-1]
               for a in runner.calls)
    assert "gh repo create wproj --private" in calls
    assert "git remote add origin https://github.com/mhollierBPT/wproj.git" in calls
    assert "git push -u origin HEAD" in calls
    assert not any("auth switch" in s or "auth login" in s or "auth logout" in s for s in calls)
    marker = json.loads((c.path / ".claude-project").read_text())
    assert marker["repo"] == "mhollierBPT/wproj" == c.repo and marker["lastCommit"] == _SHA
    local = (c.path / "CLAUDE.local.md").read_text(encoding="utf-8")
    assert "mhollierBPT/wproj" in local and "{{" not in local and "<!-- Template" not in local
    exclude = (c.path / ".git" / "info" / "exclude").read_text(encoding="utf-8").splitlines()
    assert "CLAUDE.local.md" in exclude and ".claude-project" in exclude
    assert c.warnings == []


def test_work_account_never_writes_token(fake_env, work_template):
    runner = _work_runner()
    c = create_folder(fake_env.root / "work" / "net", "wproj", "project", "W", runner, "work")
    assert c.repo == "mhollierBPT/wproj"
    for args, env in zip(runner.calls, runner.envs):
        assert not any(_TOKEN in a for a in args)
        if env and "GH_TOKEN" in env:
            assert args[0] == "gh" and args[1:3] in (["repo", "create"], ["repo", "view"])
            assert env["GH_TOKEN"] == _TOKEN
        else:
            assert not env or _TOKEN not in json.dumps(env)
    assert sum(1 for env in runner.envs if env and env.get("GH_TOKEN") == _TOKEN) == 2
    assert all(_TOKEN not in w for w in c.warnings)
    for f in _files_under(fake_env.root.parent):
        assert _TOKEN not in f.read_text(encoding="utf-8", errors="ignore"), f


def test_inherited_github_tokens_never_reach_gh_or_push(fake_env, work_template, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "gho_INHERITED")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_INHERITED")
    personal = FakeRunner({"git rev-parse HEAD": (0, _SHA + "\n", "")})
    create_folder(fake_env.root / "cc", "p", "project", "P", personal, "personal")
    work = _work_runner()
    create_folder(fake_env.root / "work" / "net", "wproj", "project", "W", work, "work")
    for runner in (personal, work):
        for args, env in zip(runner.calls, runner.envs):
            if args[0] == "gh" or args[:2] == ["git", "push"]:
                assert env is not None, args
                assert "GITHUB_TOKEN" not in env and env.get("GH_TOKEN") in (None, _TOKEN), args
    gh_create = personal.envs[personal.calls.index(["gh", "repo", "create", "p", "--private", "--source", ".",
                                                     "--push"])]
    assert "GH_TOKEN" not in gh_create and gh_create.get("PATH") == os.environ.get("PATH")


@pytest.mark.parametrize("victim", [".claude-project", "CLAUDE.local.md", "exclude"])
def test_write_errors_after_push_become_warnings(fake_env, work_template, monkeypatch, victim):
    real_write = Path.write_text

    def failing_write(self, *a, **k):
        if self.name == victim:
            raise PermissionError(13, "denied", str(self))
        return real_write(self, *a, **k)

    monkeypatch.setattr(Path, "write_text", failing_write)
    c = create_folder(fake_env.root / "work" / "net", "wproj", "project", "W", _work_runner(), "work")
    assert c.repo == "mhollierBPT/wproj"
    assert len(c.warnings) == 1 and "denied" in c.warnings[0]


def test_token_call_output_never_reaches_warning(fake_env, work_template):
    runner = _work_runner(**{"gh auth token --user mhollierBPT": (1, "gho_LEAKY", "gho_LEAKY")})
    c = create_folder(fake_env.root / "work" / "net", "wproj", "project", "W", runner, "work")
    assert len(c.warnings) == 1 and "LEAKY" not in c.warnings[0]


def test_default_runner_has_no_stdin_and_no_git_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")
    monkeypatch.setenv("X_INHERITED", "yes")
    code = ("import os, sys; data = sys.stdin.read(); "
            "print(repr(data), os.environ.get('GIT_TERMINAL_PROMPT'), os.environ.get('X_ONLY'))")
    env = {"X_ONLY": "1", "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
    r = scaffold.Runner().run([sys.executable, "-c", code], tmp_path, env=env)
    assert r.code == 0 and r.out.split() == ["''", "0", "1"]
    probe = "import os; print(os.environ.get('GIT_TERMINAL_PROMPT'), os.environ.get('X_INHERITED'))"
    assert scaffold.Runner().run([sys.executable, "-c", probe], tmp_path).out.split() == ["0", "yes"]
    assert scaffold.Runner().run(["definitely-not-a-command-xyz"], tmp_path).code == 127


def test_default_runner_creates_no_console_window(tmp_path, monkeypatch):
    """A windowed caller (the browser) must not flash a console per child: the Windows flag is passed."""
    seen = {}

    def fake_run(args, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    scaffold.Runner().run([sys.executable, "-c", "pass"], tmp_path)
    assert seen["creationflags"] == scaffold.NO_WINDOW
    if sys.platform == "win32":
        assert scaffold.NO_WINDOW == subprocess.CREATE_NO_WINDOW != 0


def test_default_runner_pipes_input(tmp_path):
    code = ("import sys; data = sys.stdin.buffer.read().decode('utf-8'); "
            "print(len(data), data.startswith('Session name: x'), data.endswith('\\u00e9' * 3))")
    r = scaffold.Runner().run([sys.executable, "-c", code], tmp_path, input="Session name: x\n" + "é" * 50_000)
    assert r.code == 0 and r.out.split() == ["50016", "True", "True"]


def test_work_account_not_signed_in_warns(fake_env, work_template):
    runner = _work_runner(**{"gh auth token --user mhollierBPT": (1, "", "no oauth token found for mhollierBPT")})
    c = create_folder(fake_env.root / "work" / "net", "wproj", "project", "W", runner, "work")
    assert not any(a[:3] == ["gh", "repo", "create"] for a in runner.calls)
    assert len(c.warnings) == 1 and "mhollierBPT" in c.warnings[0]
    assert c.repo is None and not (c.path / ".claude-project").exists()


def test_work_account_push_failure_leaves_git_only(fake_env, work_template):
    runner = _work_runner(**{"git push": (1, "", "Authentication failed")})
    c = create_folder(fake_env.root / "work" / "net", "wproj", "project", "W", runner, "work")
    assert c.repo is None and not (c.path / ".claude-project").exists()
    assert len(c.warnings) == 1 and "Authentication failed" in c.warnings[0]


def test_work_template_missing_falls_back_to_git_only(fake_env, tmp_path, monkeypatch):
    monkeypatch.setattr(scaffold, "WORK_TEMPLATE", tmp_path / "nope" / "missing.md")
    runner = _work_runner()
    c = create_folder(fake_env.root / "work" / "net", "wproj", "project", "W", runner, "work")
    assert [" ".join(a) for a in runner.calls] == ["git init -b main", "git add -A", "git commit -m chore: scaffold"]
    assert len(c.warnings) == 1 and "template" in c.warnings[0].lower()
    assert c.repo is None and not (c.path / ".claude-project").exists()
    assert not (c.path / "CLAUDE.local.md").exists()


def test_fill_work_template_substitutes_every_placeholder():
    text = ("<!-- Template: t\n     Fill in. -->\n\n"
            "This repo (`{{OWNER}}/{{NAME}}`, a fork of `{{UPSTREAM}}`) on {{DATE}}.\n"
            "- PRs: `gh pr create --repo {{OWNER}}/{{NAME}}`. For the upstream\n"
            "  repo, use `--repo {{UPSTREAM}} --head mhollierBPT:<branch>`. Ask which target before opening one.\n")
    out, leftover = scaffold.fill_work_template(text, "o", "n", "2026-11-05")
    assert leftover == []
    assert out == ("This repo (`o/n`) on 2026-11-05.\n"
                   "- PRs: `gh pr create --repo o/n`. Ask which target before opening one.\n")


def test_fill_work_template_reports_unknown_placeholder():
    _, leftover = scaffold.fill_work_template("x {{NEWTHING}} y", "o", "n", "d")
    assert leftover == ["{{NEWTHING}}"]
