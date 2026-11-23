from pathlib import Path

from launcher import launch
from launcher.launch import resume_command
from test_launch import make_row


def _codex(**kw):
    return make_row(store="codex", sid="019a-sid", backend="codex", **kw)


def test_resume_codex_bypass_and_ask():
    assert launch.CODEX_BYPASS_FLAG == "--dangerously-bypass-approvals-and-sandbox"
    r = resume_command(_codex(), None, root=Path("C:/r"))
    assert r.argv == ["codex", launch.CODEX_BYPASS_FLAG, "resume", "019a-sid"]
    assert r.cwd == Path(r"C:\proj")
    r = resume_command(_codex(), None, root=Path("C:/r"), ask_permissions=True)
    assert r.argv == ["codex", "resume", "019a-sid"]


def test_resume_codex_ignores_explicit_wrapper():
    assert resume_command(_codex(wrapper="claude6"), "claudez", root=Path("C:/r")).argv == \
        ["codex", launch.CODEX_BYPASS_FLAG, "resume", "019a-sid"]


def test_resume_codex_warns_on_missing_folder():
    r = resume_command(_codex(cwd_exists=False), None, root=Path("C:/r"))
    assert r.warnings and "no longer exists" in r.warnings[0]
