import json
import os
import uuid
from pathlib import Path

import pytest

from launcher import cli
from launcher.index import Index, remember_launch
from launcher.launch import Launch
from launcher.scaffold import Result
from launcher.suggest import Suggestion
from launcher.tui.new import NewFlow, render_new
from launcher_helpers import write_claude_session, write_qwen_session

_KEY = "sk-test-NEWFLOW"


class FakeRunner:
    """Records every command, never runs anything; every command succeeds unless scripted (longest prefix wins)."""

    def __init__(self, script=None):
        self.script = script or {}
        self.calls = []

    def run(self, args, cwd, env=None, timeout=60, input=None):
        self.calls.append(list(args))
        line = " ".join(str(a) for a in args)
        if "claude" in Path(str(args[0])).name.lower():
            return Result(1, "", "no claude in tests")  # the suggestion's Claude stage never answers
        keys = sorted((k for k in self.script if line.startswith(k)), key=len, reverse=True)
        return Result(*self.script[keys[0]]) if keys else Result(0, "", "")


class FakeHttp:
    def __init__(self, content=None):
        self.content, self.calls = content, []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append(url)
        if self.content is None:
            raise OSError("no network in tests")
        if url.endswith("/models"):
            return 200, {"data": [{"id": "qwen3-current"}]}
        return 200, {"choices": [{"message": {"content": self.content}}]}


def _session(env, cwd, sid, name, mtime, store="claude"):
    rec = [{"type": "user", "cwd": str(cwd), "timestamp": "2026-11-01T10:00:00Z",
            "message": {"role": "user", "content": name}}]
    path = (write_claude_session if store == "claude" else write_qwen_session)(env, cwd, sid, rec)
    os.utime(path, (mtime, mtime))


def _qwen_settings(env):
    (env.qwen_home / "settings.json").write_text(json.dumps({
        "model": {"name": "qwen3", "baseUrl": "http://qwen.test:30000/v1"},
        "modelProviders": {"openai": [{"id": "qwen3", "envKey": "QK"}]},
        "env": {"QK": _KEY}}), encoding="utf-8")


@pytest.fixture
def index(fake_env):
    idx = Index(fake_env, claude_live=lambda: [], pid_exists=lambda pid: False)
    idx.refresh()
    yield idx
    idx.close()


def _flow(env, index, runner=None, http=None):
    return NewFlow(env, index, runner or FakeRunner(), http or FakeHttp())


def _kinds(flow):
    return [k for k, _label, _path in flow.rows]


def _go(flow, path: Path):
    """Walk to `path` from wherever the picker is, by esc and enter (as a user would)."""
    flow.esc()
    for part in path.relative_to(flow.env.root).parts:
        flow.selected = next(i for i, (k, label, p) in enumerate(flow.rows) if k == "child" and p.name == part)
        flow.enter()
    assert flow.folder == path


def _pick(flow, kind):
    flow.selected = _kinds(flow).index(kind)
    return flow.enter()


# ---- rows ------------------------------------------------------------------------------------------------------

def test_rows_at_bucket_level_offer_start_and_new(fake_env, index):
    r = fake_env.root
    (r / "work" / "net" / "pxe").mkdir()
    (r / "work" / "net" / "forti").mkdir()
    flow = _flow(fake_env, index)
    flow.start("zzz qqq")
    _go(flow, r / "work" / "net")
    assert flow.rows == [("child", flow.rows[0][1], r / "work" / "net" / "forti"),
                         ("child", flow.rows[1][1], r / "work" / "net" / "pxe"),
                         ("start", "● start here", None), ("new", "+ new folder here…", None)]
    assert flow.breadcrumb() == "work › net"


def test_start_here_hidden_at_root_only(fake_env, index):
    flow = _flow(fake_env, index)
    flow.start("zzz qqq")
    flow.esc()
    assert flow.folder == fake_env.root
    assert _kinds(flow) == ["child", "child", "child", "new"]
    assert flow.breadcrumb() == str(fake_env.root)
    _go(flow, fake_env.root / "home")
    assert _kinds(flow)[-2:] == ["start", "new"]


def test_child_rows_carry_session_stats_and_tool(fake_env, index):
    r = fake_env.root
    now = fake_env.now()
    _session(fake_env, r / "home" / "ai", "a1", "ai old", now - 3 * 86400)
    _session(fake_env, r / "home" / "ai", "q1", "ai newer", now - 2 * 3600, store="qwen")
    index.refresh()
    flow = _flow(fake_env, index)
    flow.start("zzz qqq")
    _go(flow, r / "home")
    labels = {p.name: label for k, label, p in flow.rows if k == "child"}
    assert labels["ai"].split() == ["ai", "2", "sessions", "·", "2h", "ago", "·", "qwen"]
    assert labels["homelab"].split() == ["homelab", "no", "sessions"]


def test_name_is_required(fake_env, index):
    flow = _flow(fake_env, index)
    assert flow.step == "name"
    with pytest.raises(ValueError):
        flow.start("   ")
    flow.text = "  "
    flow.enter()
    assert flow.step == "name" and flow.error
    flow.text = "  my session  "
    flow.enter()
    assert flow.step == "picker" and flow.name == "my session"


# ---- new folder kinds --------------------------------------------------------------------------------------------

def test_new_folder_inside_a_work_bucket_asks_project_or_container(fake_env, index):
    flow = _flow(fake_env, index)
    flow.start("pxe boot")
    _go(flow, fake_env.root / "work" / "net")
    _pick(flow, "new")
    flow.enter()
    assert flow.step == "kind"
    assert [v for v, _label, _hint in flow.options()] == ["project", "container"]


def test_new_folder_at_root_is_organisational(fake_env, index):
    flow = _flow(fake_env, index)
    flow.start("lab")
    flow.esc()
    _pick(flow, "new")
    flow.enter()  # organisational only: created at once, the walk goes on inside it
    assert flow.step == "picker" and flow.folder == fake_env.root / "lab"
    assert flow.new_name is None and flow.result() is None


def test_new_folder_under_work_is_an_organisational_bucket(fake_env, index):
    flow = _flow(fake_env, index)
    flow.start("Zebra Things")
    _go(flow, fake_env.root / "work")
    _pick(flow, "new")
    assert flow.step == "folder_name" and flow.text == "zebra_things"
    assert flow.creating() == fake_env.root / "work" / "zebra_things"
    flow.enter()  # organisational is the only option: no kind prompt; created, and the walk goes on inside it
    assert flow.step == "picker" and flow.folder == fake_env.root / "work" / "zebra_things"
    assert _kinds(flow) == ["start", "new"]
    assert flow.new_name is None and flow.result() is None


def test_new_folder_under_clients_is_client(fake_env, index):
    flow = _flow(fake_env, index)
    flow.start("acme")
    _go(flow, fake_env.root / "work" / "clients")
    _pick(flow, "new")
    flow.enter()
    assert flow.step == "kind"
    assert [(v, label) for v, label, _hint in flow.options()] == [("client", "a client")]
    flow.enter()
    acme = fake_env.root / "work" / "clients" / "acme"
    assert flow.step == "picker" and flow.folder == acme and (acme / "CLAUDE.md").is_file()
    assert _kinds(flow) == ["start", "new"]


def test_new_folder_deep_asks_project_or_container_default_project(fake_env, index):
    flow = _flow(fake_env, index)
    flow.start("bifrost api")
    _go(flow, fake_env.root / "work" / "clients" / "bast")
    _pick(flow, "new")
    flow.enter()
    assert flow.step == "kind"
    assert [(v, label) for v, label, _ in flow.options()] == [("project", "a project"), ("container", "a container")]
    assert flow.selected == 0
    flow.enter()
    assert flow.new_kind == "project" and flow.step == "wrapper"


def test_bad_folder_name_is_shown_inline(fake_env, index):
    flow = _flow(fake_env, index)
    flow.start("x y")
    _go(flow, fake_env.root / "home")
    _pick(flow, "new")
    flow.text = "a/b"
    flow.enter()
    assert flow.step == "folder_name" and "path separator" in flow.error
    flow.text = "ai"  # exists already
    flow.enter()
    assert flow.step == "folder_name" and "exists" in flow.error
    flow.text = "fresh"
    flow.enter()
    assert flow.step == "kind" and flow.error == ""


# ---- suggestion ----------------------------------------------------------------------------------------------------

def test_local_suggestion_opens_picker_there(fake_env, index):
    r = fake_env.root
    _session(fake_env, r / "home" / "homelab", "h1", "homelab pxe boot", fake_env.now() - 60)
    index.refresh()
    flow = _flow(fake_env, index)
    flow.start("pxe boot rework")
    assert flow.folder == r / "home" / "homelab"
    assert flow.banner() == "suggested by history · 1 similar session lives here"


def test_suggestion_jumps_when_user_has_not_moved(fake_env, index):
    r = fake_env.root
    flow = _flow(fake_env, index)
    flow.start("zzz qqq")
    assert flow.folder == r  # root fallback
    flow.apply_suggestion(Suggestion(r / "work" / "net", "forti_tool", "high", "matched net", "qwen"))
    assert flow.folder == r / "work" / "net"
    assert flow.rows[flow.selected] == ("new_named", "+ new folder: forti_tool", None)
    assert flow.banner() == "suggested by qwen · matched net"
    flow.enter()
    assert flow.step == "folder_name" and flow.text == "forti_tool"


def test_suggestion_is_offered_not_forced_after_user_moved(fake_env, index):
    r = fake_env.root
    flow = _flow(fake_env, index)
    flow.start("zzz qqq")
    _go(flow, r / "home")
    flow.move(1)
    before = flow.rows[flow.selected]
    flow.apply_suggestion(Suggestion(r / "work" / "net", None, "high", "matched net", "qwen"))
    assert flow.folder == r / "home"
    assert flow.rows[0] == ("suggested", "★ work\\net — suggested by qwen · matched net", r / "work" / "net")
    assert flow.rows[flow.selected] == before  # the row the user had selected stays selected
    flow.selected = 0
    flow.enter()
    assert flow.folder == r / "work" / "net"
    assert "suggested" not in _kinds(flow)


def test_offer_never_steals_enter_from_what_the_user_typed(fake_env, index):
    r = fake_env.root
    flow = _flow(fake_env, index)
    flow.start("zzz qqq")
    _go(flow, r / "home")
    flow.apply_suggestion(Suggestion(r / "work" / "net", None, "high", "matched net", "qwen"))
    flow.type_filter("ai")
    assert flow.rows[0][0] == "suggested" and flow.rows[flow.selected][2] == r / "home" / "ai"
    flow.enter()
    assert flow.folder == r / "home" / "ai"
    flow.esc()  # the offer is still there at the root; a fragment must not lose to it either
    flow.type_filter("w/h")
    assert flow.rows[0][0] == "suggested" and flow.rows[flow.selected][0] == "match"
    flow.enter()
    assert flow.folder == r / "work" / "halo"
    flow.back()  # opening a level selects its first real row, not the offer
    assert flow.rows[0][0] == "suggested" and flow.selected == 1


def test_suggestion_ignored_after_the_picker(fake_env, index):
    flow = _flow(fake_env, index)
    flow.start("zzz qqq")
    _go(flow, fake_env.root / "home")
    _pick(flow, "start")
    flow.apply_suggestion(Suggestion(fake_env.root / "work" / "net", None, "high", "late", "qwen"))
    assert flow.step == "wrapper" and flow.folder == fake_env.root / "home" and flow.offered is None


def test_slash_fragment_resolves_from_the_root(fake_env, index):
    r = fake_env.root
    flow = _flow(fake_env, index)
    flow.start("zzz qqq")
    _go(flow, r / "home" / "ai")
    flow.type_filter("/w/cl/ba/bi")
    assert flow.rows == [("match", "work › clients › bast › bifrost", r / "work" / "clients" / "bast" / "bifrost")]
    flow.enter()
    assert flow.folder == r / "work" / "clients" / "bast" / "bifrost"


def test_live_model_answer_arrives_through_poll(fake_env, index):
    _qwen_settings(fake_env)
    http = FakeHttp(json.dumps({"path": "work\\halo", "new_folder": None, "confidence": "high",
                                "reason": "halo work"}))
    flow = _flow(fake_env, index, http=http)
    flow.start("zzz qqq")
    assert flow.folder == fake_env.root
    assert flow.pending.wait(5) is not None
    assert flow.poll() is True
    assert flow.folder == fake_env.root / "work" / "halo"
    assert flow.poll() is False  # applied once
    flow.pending.join(5)


def test_fragment_jump(fake_env, index):
    r = fake_env.root
    flow = _flow(fake_env, index)
    flow.start("zzz qqq")
    _go(flow, r / "work")
    flow.type_filter("cl/ba/bi")
    assert flow.rows == [("match", "work › clients › bast › bifrost", r / "work" / "clients" / "bast" / "bifrost")]
    flow.enter()
    assert flow.folder == r / "work" / "clients" / "bast" / "bifrost" and flow.filter == ""
    (r / "work" / "clients" / "bast" / "bifrost_old").mkdir()
    _go(flow, r / "work")
    flow.type_filter("cl/ba/bi")
    assert [p.name for k, _l, p in flow.rows] == ["bifrost", "bifrost_old"]
    flow.move(1)
    flow.enter()
    assert flow.folder == r / "work" / "clients" / "bast" / "bifrost_old"


def test_filter_back_and_esc(fake_env, index):
    r = fake_env.root
    flow = _flow(fake_env, index)
    flow.start("zzz qqq")
    _go(flow, r / "work")
    flow.type_filter("ha")
    assert [p.name for k, _l, p in flow.rows if k == "child"] == ["halo"]
    assert _kinds(flow)[-2:] == ["start", "new"]
    flow.backspace()
    flow.backspace()
    assert flow.filter == "" and flow.folder == r / "work"
    flow.backspace()  # empty filter: up one level
    assert flow.folder == r
    flow.backspace()  # at the root: stays
    assert flow.folder == r
    _go(flow, r / "work" / "clients" / "bast")
    flow.esc()
    assert flow.folder == r


# ---- wrapper, github, result -------------------------------------------------------------------------------------

def test_wrapper_options_and_default_per_area(fake_env, index):
    flow = _flow(fake_env, index)
    flow.start("tooling")
    _go(flow, fake_env.root / "cc")
    _pick(flow, "start")
    assert flow.step == "wrapper"
    values = [v for v, _l, _h in flow.options()]
    assert values == ["claude", "clauded", "claude6", "claudeb", "claudew", "qwen"]
    assert values[flow.selected] == "claude"
    flow.esc()
    _go(flow, fake_env.root / "home")
    _pick(flow, "start")
    assert [v for v, _l, _h in flow.options()][flow.selected] == "clauded"


def test_result_launch_start_here(fake_env, index):
    flow = _flow(fake_env, index)
    flow.start("net stuff")
    _go(flow, fake_env.root / "work" / "net")
    _pick(flow, "start")
    flow.choose_wrapper("claudeb")
    assert flow.step == "done"
    launch = flow.result()
    assert launch.cwd == fake_env.root / "work" / "net"
    assert launch.argv[:3] == ["claudeb", "-n", "net stuff"] and launch.argv[3] == "--session-id"
    assert str(uuid.UUID(launch.argv[4])) == launch.argv[4] == flow.session_id


def test_result_launch_for_new_project(fake_env, index):
    runner = FakeRunner({"gh repo create": (0, "", ""), "git rev-parse HEAD": (0, "abc123\n", "")})
    flow = _flow(fake_env, index, runner=runner)
    flow.start("Bifrost API v2")
    _go(flow, fake_env.root / "work" / "clients" / "bast" / "bifrost")
    _pick(flow, "new")
    flow.choose_new_folder("bifrost_api_v2", "project")
    assert flow.step == "wrapper"
    flow.choose_wrapper("clauded")
    assert flow.step == "github"
    assert [v for v, _l, _h in flow.options()] == ["personal", "work"] and flow.selected == 0
    flow.choose_github("personal")
    assert flow.step == "done"
    target = fake_env.root / "work" / "clients" / "bast" / "bifrost" / "bifrost_api_v2"
    launch = flow.result()
    assert isinstance(launch, Launch) and launch.cwd == target
    assert launch.argv[:3] == ["clauded", "-n", "Bifrost API v2"]
    assert (target / "plan.md").is_file()
    assert ["gh", "repo", "create", "bifrost_api_v2", "--private", "--source", ".", "--push"] in runner.calls


def test_result_carries_scaffold_warnings(fake_env, index):
    runner = FakeRunner({"git init": (1, "", "boom")})
    flow = _flow(fake_env, index, runner=runner)
    flow.start("p")
    _go(flow, fake_env.root / "home")
    _pick(flow, "new")
    flow.choose_new_folder("proj_x", "project")
    flow.choose_wrapper("clauded")
    flow.choose_github("personal")
    launch = flow.result()
    assert any("git init failed" in w for w in launch.warnings)


def test_container_is_created_and_the_walk_continues_inside(fake_env, index):
    runner = FakeRunner()
    flow = _flow(fake_env, index, runner=runner)
    flow.start("grouping")
    _go(flow, fake_env.root / "home")
    _pick(flow, "new")
    flow.submit_folder_name("group_x")
    flow.selected = 1  # a container
    assert flow.creating() == fake_env.root / "home" / "group_x"
    flow.enter()
    group = fake_env.root / "home" / "group_x"
    assert group.is_dir() and flow.step == "picker" and flow.folder == group  # no launch yet
    assert _kinds(flow) == ["start", "new"] and flow.result() is None
    _pick(flow, "new")  # keep walking: a project inside the container
    flow.choose_new_folder("inner", "project")
    flow.choose_wrapper("clauded")
    flow.choose_github("personal")
    assert flow.result().cwd == group / "inner"
    assert not (group / ".git").exists() and not (group / "plan.md").exists()


def test_container_then_start_here_launches_in_it(fake_env, index):
    runner = FakeRunner()
    flow = _flow(fake_env, index, runner=runner)
    flow.start("grouping")
    _go(flow, fake_env.root / "home")
    _pick(flow, "new")
    flow.choose_new_folder("group_y", "container")
    _pick(flow, "start")
    flow.choose_wrapper("clauded")
    assert flow.result().cwd == fake_env.root / "home" / "group_y"
    assert not [c for c in runner.calls if c[0] in ("git", "gh")]


def test_folder_taken_at_create_returns_to_name_prompt(fake_env, index):
    flow = _flow(fake_env, index)
    flow.start("late clash")
    _go(flow, fake_env.root / "home")
    _pick(flow, "new")
    flow.choose_new_folder("late", "project")
    (fake_env.root / "home" / "late").mkdir()  # someone else made it meanwhile
    flow.choose_wrapper("clauded")
    flow.choose_github("personal")
    assert flow.step == "folder_name" and flow.error and flow.result() is None
    flow.submit_folder_name("late2")
    (fake_env.root / "home" / "late2").mkdir()
    flow.choose_kind("container")
    assert flow.step == "folder_name" and flow.error.startswith("could not create the folder")
    assert flow.text == "late2"


def test_summary_matches_the_launch_flags(fake_env, index):
    flow = _flow(fake_env, index)
    flow.start("net stuff")
    _go(flow, fake_env.root / "work" / "net")
    _pick(flow, "start")
    target = fake_env.root / "work" / "net"
    assert flow.summary() == [f'→ cd {target} ; clauded -n "net stuff"']
    flow.selected = [v for v, _l, _h in flow.options()].index("qwen")
    assert flow.summary() == [f"→ cd {target} ; qwen"]  # Qwen Code has no -n
    flow.choose_wrapper("qwen")
    assert flow.result().argv[:2] == ["qwen", "--session-id"]


def test_result_none_until_done(fake_env, index):
    flow = _flow(fake_env, index)
    assert flow.result() is None
    flow.start("x")
    assert flow.result() is None


def test_render_new_shows_breadcrumb_banner_and_rows(fake_env, index):
    r = fake_env.root
    flow = _flow(fake_env, index)
    flow.start("zzz qqq")
    flow.apply_suggestion(Suggestion(r / "work" / "net", "forti_tool", "high", "matched net", "qwen"))
    text = "".join(t for _s, t in render_new(flow, 100, 30))
    assert "Session name: zzz qqq" in text
    assert "suggested by qwen · matched net" in text
    assert "work › net" in text and "▶ + new folder: forti_tool" in text and "● start here" in text


# ---- launched.json sidecar ----------------------------------------------------------------------------------------

def test_sidecar_sets_wrapper_on_next_refresh(fake_env):
    remember_launch(fake_env.data_dir, "claude", "sid-1", "claudeb")
    remember_launch(fake_env.data_dir, "claude", "sid-2", "claude6")
    idx = Index(fake_env, claude_live=lambda: [], pid_exists=lambda pid: False)
    idx.refresh()  # neither transcript exists yet: both entries are kept
    assert json.loads((fake_env.data_dir / "launched.json").read_text(encoding="utf-8")) == {
        "claude:sid-1": "claudeb", "claude:sid-2": "claude6"}
    _session(fake_env, fake_env.root / "cc", "sid-1", "hello", fake_env.now())
    idx.refresh()
    assert idx.row("claude", "sid-1").wrapper == "claudeb"
    assert json.loads((fake_env.data_dir / "launched.json").read_text(encoding="utf-8")) == {
        "claude:sid-2": "claude6"}
    idx.close()


def test_sidecar_never_overrides_a_recorded_wrapper(fake_env):
    _session(fake_env, fake_env.root / "cc", "sid-1", "hello", fake_env.now())
    idx = Index(fake_env, claude_live=lambda: [], pid_exists=lambda pid: False)
    idx.refresh()
    idx.record_launch("claude", "sid-1", "claude6")
    remember_launch(fake_env.data_dir, "claude", "sid-1", "claudeb")
    idx.refresh()
    assert idx.row("claude", "sid-1").wrapper == "claude6"
    assert not (fake_env.data_dir / "launched.json").exists()
    idx.close()


def test_sidecar_unreadable_is_ignored(fake_env):
    fake_env.data_dir.mkdir(parents=True, exist_ok=True)
    (fake_env.data_dir / "launched.json").write_text("{not json", encoding="utf-8")
    idx = Index(fake_env, claude_live=lambda: [], pid_exists=lambda pid: False)
    idx.refresh()
    remember_launch(fake_env.data_dir, "claude", "s", "claude")  # replaces the broken file
    assert json.loads((fake_env.data_dir / "launched.json").read_text(encoding="utf-8")) == {"claude:s": "claude"}
    idx.close()


# ---- cli -----------------------------------------------------------------------------------------------------------

def test_cli_new_joins_name_words_and_writes_sidecar(fake_env, index, capsys):
    seen = {}

    def new_app(env, idx, name):
        seen["name"] = name
        flow = _flow(env, idx)
        flow.start(name)
        _go(flow, env.root / "home")
        _pick(flow, "start")
        flow.choose_wrapper("claudew")
        return flow.result()

    assert cli.main(["new", "pxe", "boot", "order"], env=fake_env, index=index, new_app=new_app) == 0
    assert seen["name"] == "pxe boot order"
    out = json.loads(capsys.readouterr().out)
    assert out["cd"] == str(fake_env.root / "home") and out["run"][:3] == ["claudew", "-n", "pxe boot order"]
    sid = out["run"][4]
    assert json.loads((fake_env.data_dir / "launched.json").read_text(encoding="utf-8")) == {f"claude:{sid}": "claudew"}


def test_cli_new_without_name_passes_none_and_quits_quietly(fake_env, index, capsys):
    seen = {}

    def new_app(env, idx, name):
        seen["name"] = name
        return None

    assert cli.main(["new"], env=fake_env, index=index, new_app=new_app) == 0
    assert seen["name"] is None and capsys.readouterr().out == ""
    assert not (fake_env.data_dir / "launched.json").exists()


def test_cli_n_from_resume_list_runs_new_flow(fake_env, index, capsys):
    from launcher.tui.resume import NEW
    launch = Launch(fake_env.root / "cc", ["qwen", "-n", "x", "--session-id", "q-1"], [])
    assert cli.main([], env=fake_env, index=index, app=lambda e, i: NEW,
                    new_app=lambda e, i, name: launch) == 0
    assert json.loads(capsys.readouterr().out)["run"][0] == "qwen"
    assert json.loads((fake_env.data_dir / "launched.json").read_text(encoding="utf-8")) == {"qwen:q-1": "qwen"}
