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
    rec = [{"type": "user", "cwd": str(cwd),
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
    assert flow.new_kind == "project" and flow.step == "confirm"


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

def test_weak_local_suggestion_is_a_row_at_the_root(fake_env, index):
    r = fake_env.root
    pxe = r / "home" / "homelab" / "pxe"
    pxe.mkdir()  # home\homelab itself is a place, never a project answer
    _session(fake_env, pxe, "h1", "homelab pxe boot", fake_env.now() - 60)
    index.refresh()
    flow = _flow(fake_env, index)
    flow.start("pxe boot rework")
    assert flow.pending.local.path == pxe and not flow.pending.local.strong
    assert flow.folder == r and flow.banner() == ""  # round 2: only a strong local answer moves the picker
    assert flow.rows[0] == ("alt", "≈ home\\homelab\\pxe", pxe) and flow.rows[flow.selected][0] == "child"
    flow.pending.join(5)


def test_suggestion_is_offered_not_forced_after_user_moved(fake_env, index):
    r = fake_env.root
    flow = _flow(fake_env, index)
    flow.start("zzz qqq")
    _go(flow, r / "home")
    flow.move(1)
    before = flow.rows[flow.selected]
    flow.apply_suggestion(Suggestion(r / "work" / "net", None, "high", "matched net", "qwen"))
    assert flow.folder == r / "home"
    assert flow.rows[0] == ("suggested", "★ work\\net", r / "work" / "net")
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
    assert flow.rows[0][0] == "suggested"
    flow.type_filter("ai")  # while a filter is typed the suggestion rows step aside
    assert "suggested" not in _kinds(flow) and flow.rows[flow.selected][2] == r / "home" / "ai"
    flow.enter()
    assert flow.folder == r / "home" / "ai"
    flow.esc()  # the offer is still there at the root; a fragment must not lose to it either
    assert flow.rows[0][0] == "suggested"
    flow.type_filter("w/h")
    assert "suggested" not in _kinds(flow) and flow.rows[flow.selected][0] == "match"
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
    assert flow.step == "confirm" and flow.folder == fake_env.root / "home" and flow.offered is None


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
    from launcher.suggest import prepare
    halo = [c.rel for c in prepare(fake_env, index.rows(), "zzz qqq").choices].index("work\\halo") + 1
    http = FakeHttp(json.dumps({"choice": halo, "new_folder": None, "confidence": "high", "reason": "halo work"}))
    flow = _flow(fake_env, index, http=http)
    flow.start("zzz qqq")
    assert flow.folder == fake_env.root
    assert flow.pending.wait(5) is not None
    assert flow.poll() is True
    assert flow.folder == fake_env.root  # round 2: the answer is the ★ row, it does not move the picker
    # work\halo is a place with no sessions and the answer names no folder: c0cb66b marks it needs_name
    assert flow.rows[0] == ("suggested", "★ work\\halo + new folder…", fake_env.root / "work" / "halo")
    assert flow.rows[flow.selected][0] == "child"
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

def test_confirm_preselects_the_wrapper_per_area_and_w_cycles_in_order(fake_env, index):
    flow = _flow(fake_env, index)
    flow.start("tooling")
    _go(flow, fake_env.root / "cc")
    _pick(flow, "start")
    assert flow.step == "confirm" and flow.wrapper == "claude" and flow.github is None
    seen = [flow.wrapper]
    for _ in range(6):
        flow.cycle_wrapper()
        seen.append(flow.wrapper)
    assert seen == ["claude", "clauded", "claude6", "claudeb", "claudew", "qwen", "claude"]
    flow.cycle_github()  # start here creates nothing: there is no GitHub choice to cycle
    assert flow.github is None
    flow.esc()
    assert flow.step == "picker"
    _go(flow, fake_env.root / "home")
    _pick(flow, "start")
    assert flow.wrapper == "clauded"


def test_result_launch_start_here(fake_env, index):
    flow = _flow(fake_env, index)
    flow.start("net stuff")
    _go(flow, fake_env.root / "work" / "net")
    _pick(flow, "start")
    flow.choose_wrapper("claudeb")
    flow.enter()
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
    assert flow.step == "confirm" and (flow.wrapper, flow.github) == ("clauded", "personal")
    assert not any(c[0] == "gh" for c in runner.calls)  # nothing is created before enter
    flow.enter()
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
    flow.enter()
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
    flow.enter()
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
    flow.enter()
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
    flow.enter()
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
    flow.choose_wrapper("qwen")
    assert flow.summary() == [f"→ cd {target} ; qwen"]  # Qwen Code has no -n
    flow.enter()
    assert flow.result().argv[:2] == ["qwen", "--session-id"]


def test_result_none_until_done(fake_env, index):
    flow = _flow(fake_env, index)
    assert flow.result() is None
    flow.start("x")
    assert flow.result() is None


def _run_keys(flow, keys):
    """Run the real prompt_toolkit app for `flow` on a pipe, feeding `keys`; returns its result."""
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from launcher.tui.app import build_new_app
    with create_pipe_input() as pipe:
        app = build_new_app(flow, output=DummyOutput(), input=pipe)
        pipe.send_text(keys)
        return app.run()


def test_picker_q_is_a_filter_character_and_ctrl_c_quits(fake_env, index):
    (fake_env.root / "home" / "quartz").mkdir()
    flow = _flow(fake_env, index)
    flow.start("zzz qqq")
    _go(flow, fake_env.root / "home")
    assert _run_keys(flow, "qu\x03") is None  # ctrl-c aborts
    assert flow.step == "picker" and flow.filter == "qu"
    assert [p.name for k, _l, p in flow.rows if k == "child"] == ["quartz"]


def test_picker_footer_names_ctrl_c_not_q(fake_env, index):
    flow = _flow(fake_env, index)
    flow.start("zzz qqq")
    text = "".join(t for _s, t in render_new(flow, 200, 40))
    assert "ctrl-c cancel" in text and "q quit" not in text


def test_render_new_shows_breadcrumb_banner_and_rows(fake_env, index):
    r = fake_env.root
    flow = _flow(fake_env, index)
    flow.start("zzz qqq")
    flow.apply_suggestion(Suggestion(r / "work" / "net", "forti_tool", "high", "matched net", "qwen"))
    text = "".join(t for _s, t in render_new(flow, 100, 30))
    assert "★ work\\net + new folder: forti_tool" in text
    flow.selected = 0
    flow.enter()  # the ★ row
    text = "".join(t for _s, t in render_new(flow, 100, 30))
    assert "Session name: zzz qqq" in text
    assert "suggested by qwen · matched net" in text
    assert "work › net" in text and "▶ + new folder: forti_tool" in text and "● start here" in text


# ---- round 2: typed new folder, selection identity, suggestions block, folding, footer, flow log ----------------

class _FakePending:
    """A `suggest()` result with a fixed local answer and (optionally) a model answer, no thread."""

    def __init__(self, local, model=None):
        self.local, self.model = local, model

    def wait(self, timeout=None):
        return self.model

    def join(self, timeout=None):
        return True


def _flow_with(env, index, local=None, model=None, runner=None):
    pending = _FakePending(local or Suggestion(env.root, None, "low", "no suggestion", "root"), model)
    flow = NewFlow(env, index, runner or FakeRunner(), FakeHttp(), suggest_fn=lambda *a, **k: pending)
    return flow, pending


def _flowlog(env):
    path = env.data_dir / "flow.log"
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def _outcomes(env):
    path = env.data_dir / "suggest.log"
    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
    return [x for x in lines if x.get("stage") == "outcome"]


def test_typed_text_matching_no_child_becomes_the_new_folder(fake_env, index):
    r = fake_env.root
    ninja = r / "work" / "ninja"
    (ninja / "scripts").mkdir(parents=True)
    flow, _p = _flow_with(fake_env, index)
    flow.start("Ninja RMM Alert Review")
    _go(flow, ninja)
    flow.type_filter("alert review")
    assert flow.rows[0] == ("new_typed", "+ new folder: alert_review", None)
    assert flow.selected == 0
    flow.enter()
    assert flow.step == "folder_name" and flow.text == "alert_review"  # not the session-name slug
    flow.esc()  # back in the picker, the typed text is still there
    assert flow.step == "picker" and flow.filter == "alert review" and flow.rows[flow.selected][0] == "new_typed"


def test_typed_new_folder_in_a_level_without_children_never_defaults_to_start_here(fake_env, index):
    r = fake_env.root
    flow, _p = _flow_with(fake_env, index)
    flow.start("Ninja RMM Alert Review")
    _go(flow, r / "home" / "ai")  # no children: start here is the default with no filter
    assert flow.rows[flow.selected][0] == "start"
    for ch in "alert review":
        flow.type_filter(flow.filter + ch)
        assert flow.rows[flow.selected][0] != "start"
    assert flow.rows[flow.selected] == ("new_typed", "+ new folder: alert_review", None)
    flow.enter()
    assert flow.step == "folder_name" and flow.text == "alert_review"
    flow.esc()
    flow.type_filter("!!")  # nothing usable as a name: still not start here
    assert "new_typed" not in _kinds(flow) and flow.rows[flow.selected][0] == "new"


def test_typed_new_folder_is_offered_after_matching_children(fake_env, index):
    r = fake_env.root
    flow, _p = _flow_with(fake_env, index)
    flow.start("x")
    _go(flow, r / "work")
    flow.type_filter("ha")
    assert _kinds(flow) == ["child", "new_typed", "start", "new"]
    assert flow.rows[1][1] == "+ new folder: ha" and flow.rows[flow.selected][2] == r / "work" / "halo"
    flow.type_filter("halo")  # an existing folder's exact name is not offered as new
    assert "new_typed" not in _kinds(flow)
    flow.type_filter("w/h")  # a fragment jump is not a folder name
    assert "new_typed" not in _kinds(flow)


def test_offer_arrival_keeps_the_selected_child(fake_env, index):
    r = fake_env.root
    flow, _p = _flow_with(fake_env, index)
    flow.start("x")
    _go(flow, r / "work")
    first = flow.rows[flow.selected]
    assert first[0] == "child"
    flow.apply_suggestion(Suggestion(r / "home" / "ai", None, "high", "ai", "qwen"))
    assert flow.rows[0][0] == "suggested" and flow.rows[flow.selected] == first  # the ★ never takes the cursor
    flow.move(1)
    second = flow.rows[flow.selected]
    flow.apply_suggestion(Suggestion(r / "home" / "homelab", None, "high", "lab", "qwen"))
    assert flow.rows[flow.selected] == second
    assert flow.folder == r / "work"  # a model answer never moves the picker


def test_model_answer_is_a_row_and_enter_preselects_its_new_folder(fake_env, index):
    r = fake_env.root
    flow, _p = _flow_with(fake_env, index)
    flow.start("zzz qqq")
    assert flow.folder == r
    flow.apply_suggestion(Suggestion(r / "work" / "net", "forti_tool", "high", "matched net", "qwen"))
    assert flow.folder == r
    assert flow.rows[0] == ("suggested", "★ work\\net + new folder: forti_tool", r / "work" / "net")
    assert flow.rows[flow.selected][0] == "child"
    flow.selected = 0
    flow.enter()
    assert flow.folder == r / "work" / "net"
    assert "suggested" not in _kinds(flow)  # the current folder is never suggested
    assert flow.rows[flow.selected] == ("new_named", "+ new folder: forti_tool", None)
    assert flow.banner() == "suggested by qwen · matched net"
    flow.enter()
    assert flow.step == "folder_name" and flow.text == "forti_tool"


def test_suggestions_block_local_alternatives_then_model(fake_env, index):
    r = fake_env.root
    a, b, c, d = (r / "work" / "halo", r / "home" / "ai", r / "home" / "homelab", r / "cc")
    local = Suggestion(a, None, "medium", "matches halo", "history", alternatives=[b, c])
    flow, pending = _flow_with(fake_env, index, local=local)
    flow.start("halo thing")
    assert flow.folder == r  # not strong: the picker opens at the root
    assert flow.rows[:3] == [("alt", "≈ work\\halo", a), ("alt", "≈ home\\ai", b), ("alt", "≈ home\\homelab", c)]
    assert flow.rows[3][0] == "child" and flow.selected == 3
    flow.selected = 1
    flow.enter()
    assert flow.folder == b and [k for k in _kinds(flow) if k == "alt"] == ["alt", "alt"]  # b hidden here
    pending.model = Suggestion(d, None, "high", "tooling", "qwen", alternatives=[a, c])
    assert flow.poll() is True
    assert flow.folder == b
    assert flow.rows[:3] == [("suggested", "★ cc", d), ("alt", "≈ work\\halo", a), ("alt", "≈ home\\homelab", c)]
    assert flow.poll() is False


def test_strong_local_opens_the_picker_there(fake_env, index):
    r = fake_env.root
    local = Suggestion(r / "work" / "net", None, "high", "matches forti, vpn", "history", strong=True)
    flow, _p = _flow_with(fake_env, index, local=local)
    flow.start("forti vpn")
    assert flow.folder == r / "work" / "net"
    assert flow.banner() == "suggested by history · matches forti, vpn"
    flow.esc()
    assert flow.rows[0] == ("alt", "≈ work\\net", r / "work" / "net")


def test_children_are_ordered_by_last_activity(fake_env, index):
    r = fake_env.root
    now = fake_env.now()
    _session(fake_env, r / "work" / "halo", "s1", "halo", now - 5 * 86400)
    _session(fake_env, r / "work" / "net", "s2", "net", now - 3600)
    index.refresh()
    flow, _p = _flow_with(fake_env, index)
    flow.start("x")
    _go(flow, r / "work")
    assert [p.name for k, _l, p in flow.rows if k == "child"] == ["net", "halo", "clients", "vendors"]


def test_long_levels_fold_and_expand(fake_env, index):
    many = fake_env.root / "home" / "many"
    for i in range(1, 16):
        (many / f"f{i:02d}").mkdir(parents=True)
    flow, _p = _flow_with(fake_env, index)
    flow.start("x")
    _go(flow, many)
    assert _kinds(flow) == ["child"] * 12 + ["more", "start", "new"]
    assert flow.rows[12] == ("more", "… 3 more (type to filter, enter to show all)", None)
    flow.selected = 12
    flow.enter()
    assert flow.folder == many and _kinds(flow) == ["child"] * 15 + ["start", "new"]
    assert flow.rows[flow.selected][2] == many / "f13"
    flow.back()
    _go(flow, many)
    flow.type_filter("f15")  # a filter searches every child, folded or not
    assert [p.name for k, _l, p in flow.rows if k == "child"] == ["f15"]


def test_picker_footer_names_the_exits_and_wraps(fake_env, index):
    flow, _p = _flow_with(fake_env, index)
    flow.start("zzz qqq")
    from launcher.tui.new import PICKER_FOOTER
    assert PICKER_FOOTER == ("enter select · type to filter or name a new folder · backspace clear filter / up · "
                             "esc top · /path jump · ctrl-c cancel")
    wide = "".join(t for _s, t in render_new(flow, 200, 40)).splitlines()
    assert PICKER_FOOTER in wide
    for width in (100, 60):
        lines = "".join(t for _s, t in render_new(flow, width, 40)).splitlines()
        start = next(i for i, x in enumerate(lines) if x.startswith("enter select"))
        footer = [x for x in lines[start:] if x]
        assert all(len(x) <= width for x in footer) and len(footer) >= 2
        assert " · ".join(footer) == PICKER_FOOTER  # wrapped, nothing cut


def test_flow_log_and_outcome_for_a_typed_new_project(fake_env, index):
    r = fake_env.root
    ninja = r / "work" / "ninja"
    ninja.mkdir()
    model = Suggestion(ninja, "alert_review", "high", "ninja alerts", "qwen")
    flow, _p = _flow_with(fake_env, index, model=model)
    flow.start("Ninja RMM Alert Review")
    flow.poll()
    _go(flow, ninja)
    flow.type_filter("alert review")
    flow.enter()
    flow.enter()  # folder name
    assert flow.step == "kind"
    flow.enter()  # project
    flow.choose_wrapper("clauded")
    flow.choose_github("personal")
    flow.enter()
    assert flow.step == "done"
    events = _flowlog(fake_env)
    assert [e["event"] for e in events if e["event"] != "open"] == [
        "start", "filter-enter", "new-folder", "kind", "launch"]
    assert all({"ts", "event", "folder", "kind", "filter", "step"} <= set(e) for e in events)
    fe = next(e for e in events if e["event"] == "filter-enter")
    assert fe["folder"] == "work\\ninja" and fe["kind"] == "new_typed" and fe["filter"] == "alert review"
    nf = next(e for e in events if e["event"] == "new-folder")
    assert nf["name"] == "alert_review" and nf["kind"] == "new_typed"
    assert next(e for e in events if e["event"] == "kind")["value"] == "project"
    launch = next(e for e in events if e["event"] == "launch")
    assert launch["folder"] == "work\\ninja" and launch["new_folder"] == "alert_review"
    assert launch["wrapper"] == "clauded"
    assert _outcomes(fake_env) == [{"ts": fake_env.now(), "stage": "outcome", "name": "Ninja RMM Alert Review",
                                    "suggested": "work\\ninja", "suggested_source": "qwen",
                                    "chosen": "work\\ninja", "new_folder": "alert_review", "match": True}]


def test_outcome_uses_the_local_suggestion_before_a_model_answer(fake_env, index):
    r = fake_env.root
    local = Suggestion(r / "work" / "halo", None, "medium", "matches halo", "history")
    flow, _p = _flow_with(fake_env, index, local=local)
    flow.start("halo")
    _go(flow, r / "home")
    _pick(flow, "start")
    flow.choose_wrapper("clauded")
    flow.enter()
    out = _outcomes(fake_env)
    assert len(out) == 1 and out[0]["suggested"] == "work\\halo" and out[0]["chosen"] == "home"
    assert out[0]["new_folder"] is None and out[0]["match"] is False


def test_ctrl_c_logs_cancel(fake_env, index):
    flow, _p = _flow_with(fake_env, index)
    flow.start("zzz qqq")
    assert _run_keys(flow, "ab\x03") is None
    events = _flowlog(fake_env)
    assert events[-1]["event"] == "cancel" and events[-1]["filter"] == "ab" and events[-1]["step"] == "picker"


# ---- round 2 review fixes -----------------------------------------------------------------------------------------

def test_typed_text_wins_over_a_preselected_name(fake_env, index):
    r = fake_env.root
    flow, _p = _flow_with(fake_env, index)
    flow.start("zzz qqq")
    flow.apply_suggestion(Suggestion(r / "work" / "net", "forti_tool", "high", "net", "qwen"))
    flow.selected = 0
    flow.enter()  # the ★ row: + new folder: forti_tool is selected
    assert flow.rows[flow.selected] == ("new_named", "+ new folder: forti_tool", None)
    flow.type_filter("vpn certs")
    assert flow.rows[flow.selected] == ("new_typed", "+ new folder: vpn_certs", None)
    flow.enter()
    assert flow.step == "folder_name" and flow.text == "vpn_certs"


def test_filter_never_keeps_new_or_start_selected(fake_env, index):
    r = fake_env.root
    flow, _p = _flow_with(fake_env, index)
    flow.start("x")
    _go(flow, r / "work")
    flow.selected = _kinds(flow).index("new")
    flow.type_filter("ha")
    assert flow.rows[flow.selected][2] == r / "work" / "halo"
    flow.type_filter("")
    flow.selected = _kinds(flow).index("start")
    flow.type_filter("zz")
    assert flow.rows[flow.selected][0] == "new_typed"


def test_esc_back_to_the_picker_restores_the_row_the_user_left_from(fake_env, index):
    r = fake_env.root
    local = Suggestion(r / "work" / "halo", None, "medium", "halo", "history", alternatives=[r / "home" / "ai"])
    flow, _p = _flow_with(fake_env, index, local=local)
    flow.start("halo thing")
    _go(flow, r / "work" / "net")
    assert _kinds(flow)[:2] == ["alt", "alt"]
    _pick(flow, "new")
    flow.enter()  # name
    assert flow.step == "kind"
    flow.selected = 1  # container: an option index that is a ≈ row's index in the picker
    flow.esc()
    flow.esc()
    assert flow.step == "picker" and flow.rows[flow.selected][0] == "new"
    _pick(flow, "start")
    assert flow.step == "confirm"
    flow.esc()
    assert flow.step == "picker" and flow.rows[flow.selected][0] == "start"


def test_cursor_on_alt_follows_to_the_star_for_the_same_folder(fake_env, index):
    r = fake_env.root
    a, b = r / "work" / "halo", r / "home" / "ai"
    local = Suggestion(a, None, "medium", "halo", "history", alternatives=[b])
    flow, pending = _flow_with(fake_env, index, local=local)
    flow.start("halo thing")
    flow.selected = 0
    assert flow.rows[0] == ("alt", "≈ work\\halo", a)
    pending.model = Suggestion(a, None, "high", "halo", "qwen")  # no alternatives of its own
    flow.poll()
    assert flow.rows[:2] == [("suggested", "★ work\\halo", a), ("alt", "≈ home\\ai", b)]  # local ≈ kept
    assert flow.selected == 0
    flow.selected = 1
    flow.apply_suggestion(Suggestion(r / "cc", None, "high", "cc", "qwen", alternatives=[r / "home" / "homelab"]))
    assert "≈ home\\ai" not in [l for _k, l, _p in flow.rows]
    assert flow.rows[flow.selected][0] == "child"  # its row is gone: the default row


def test_needs_name_star_opens_with_new_folder_selected(fake_env, index):
    r = fake_env.root
    flow, _p = _flow_with(fake_env, index)
    flow.start("Vendor Portal Audit")
    flow.apply_suggestion(Suggestion(r / "work" / "vendors", None, "medium", "vendor", "qwen", needs_name=True))
    assert flow.rows[0] == ("suggested", "★ work\\vendors + new folder…", r / "work" / "vendors")
    flow.selected = 0
    flow.enter()
    assert flow.folder == r / "work" / "vendors" and flow.rows[flow.selected][0] == "new"
    flow.enter()
    assert flow.step == "folder_name" and flow.text == "vendor_portal_audit"


def test_unusable_typed_name_offers_plain_new_folder(fake_env, index):
    r = fake_env.root
    flow, _p = _flow_with(fake_env, index)
    flow.start("x")
    _go(flow, r / "home" / "ai")
    for typed in ("...", ".hidden"):  # check_name rejects both
        flow.type_filter(typed)
        assert "new_typed" not in _kinds(flow) and flow.rows[flow.selected][0] == "new"


def test_backspace_and_esc_at_the_root_do_nothing(fake_env, index):
    r = fake_env.root
    flow, _p = _flow_with(fake_env, index)
    flow.start("x")
    flow.move(2)
    before, n = flow.selected, len(_flowlog(fake_env))
    flow.backspace()
    flow.esc()
    assert flow.folder == r and flow.selected == before and len(_flowlog(fake_env)) == n


def test_filter_shows_the_suggestions_hidden_line(fake_env, index):
    r = fake_env.root
    local = Suggestion(r / "work" / "halo", None, "medium", "halo", "history")
    flow, _p = _flow_with(fake_env, index, local=local)
    flow.start("halo")
    hint = "suggestions hidden while filtering · backspace to clear"
    assert hint not in "".join(t for _s, t in render_new(flow, 100, 30))
    flow.type_filter("w")
    lines = "".join(t for _s, t in render_new(flow, 100, 30)).splitlines()
    crumb = next(i for i, x in enumerate(lines) if x.startswith(str(r)))
    assert lines[crumb + 1].strip() == hint


def test_prefix_matches_come_before_substring_matches(fake_env, index):
    r = fake_env.root
    now = fake_env.now()
    ninja = r / "work" / "ninja"
    for name in ("ninja_alerts", "alerts_archive"):
        (ninja / name).mkdir(parents=True)
    _session(fake_env, ninja / "ninja_alerts", "s1", "alerts", now - 60)  # the more recent one
    index.refresh()
    flow, _p = _flow_with(fake_env, index)
    flow.start("x")
    _go(flow, ninja)
    flow.selected = _kinds(flow).index("new")  # not a child: the filter picks the first match
    flow.type_filter("alerts")
    assert [(k, p.name if p else None) for k, _l, p in flow.rows][:3] == [
        ("child", "alerts_archive"), ("child", "ninja_alerts"), ("new_typed", None)]
    assert flow.selected == 0
    # a lower match selected before typing does not keep the cursor: it lands on the first prefix match
    flow.type_filter("")
    flow.selected = [p.name if p else None for _k, _l, p in flow.rows].index("ninja_alerts")
    flow.type_filter("alerts")
    assert flow.rows[flow.selected][2].name == "alerts_archive"


def test_cursor_goes_to_the_first_match_on_every_filter_keystroke(fake_env, index):
    ninja = fake_env.root / "work" / "ninja"
    for name in ("alpha", "alpine", "beta"):
        (ninja / name).mkdir(parents=True)
    index.refresh()
    flow, _p = _flow_with(fake_env, index)
    flow.start("x")
    _go(flow, ninja)

    def on():
        kind, _label, path = flow.rows[flow.selected]
        return kind, path.name if path else None

    flow.type_filter("a")
    flow.selected = [p.name if p else None for _k, _l, p in flow.rows].index("alpine")
    flow.type_filter("al")  # alpine was selected: both still match, the cursor returns to the first
    first = flow.rows[0][2].name
    assert on() == ("child", first)
    flow.type_filter("alq")  # no child matches: the typed new folder
    assert on() == ("new_typed", None)
    flow.type_filter("")  # emptied: the unfiltered level, not a start-here or new-folder row
    assert flow.rows[flow.selected][0] == "child"
    # a rebuild that is not a filter change keeps the selected row by identity
    flow.selected = [p.name if p else None for _k, _l, p in flow.rows].index("beta")
    flow._refresh()
    assert on() == ("child", "beta")


def test_q_on_the_kind_step_logs_one_cancel(fake_env, index):
    flow, _p = _flow_with(fake_env, index)
    flow.start("x")
    _go(flow, fake_env.root / "work" / "net")
    _pick(flow, "new")
    flow.enter()  # the folder name: the session-name slug
    assert flow.step == "kind"
    assert _run_keys(flow, "q") is None
    assert [e["event"] for e in _flowlog(fake_env)].count("cancel") == 1


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
        flow.enter()
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


# ---- the confirm step (round 2, task 4) ---------------------------------------------------------------------------

CONFIRM_KEYS = "enter launch · w wrapper · g github · esc back · ctrl-c cancel"


def _text(flow, width=100, height=30):
    return "".join(t for _s, t in render_new(flow, width, height))


def _to_new_project(flow, parent, name="export_job"):
    _go(flow, parent)
    _pick(flow, "new")
    flow.choose_new_folder(name, "project")
    assert flow.step == "confirm"


def test_confirm_screen_for_start_here_in_an_existing_folder(fake_env, index):
    flow = _flow(fake_env, index)
    flow.start("net stuff")
    _go(flow, fake_env.root / "work" / "net")
    _pick(flow, "start")
    target = fake_env.root / "work" / "net"
    assert _text(flow, 400).splitlines() == [
        "Session name: net stuff", "", "work › net", "folder   existing", "wrapper  clauded   (w to change)",
        f'runs     cd {target} ; clauded -n "net stuff"', "", CONFIRM_KEYS]


def test_confirm_screen_for_a_new_project_under_work_net(fake_env, index):
    flow = _flow(fake_env, index)
    flow.start("vpn export")
    _to_new_project(flow, fake_env.root / "work" / "net", "vpn_export")
    target = fake_env.root / "work" / "net" / "vpn_export"
    assert _text(flow, 400).splitlines() == [
        "Session name: vpn export", "", "work › net › vpn_export", "folder   new project   vpn_export",
        "wrapper  clauded   (w to change)", "github   personal   (g to change)",
        f'runs     cd {target} ; clauded -n "vpn export"', "", CONFIRM_KEYS]


def test_confirm_runs_line_wraps_without_losing_text(fake_env, index):
    flow = _flow(fake_env, index)
    flow.start("a fairly long session name for a narrow terminal")
    _go(flow, fake_env.root / "work" / "net")
    _pick(flow, "start")
    lines = _text(flow, width=40).splitlines()
    start = next(i for i, x in enumerate(lines) if x.startswith("runs     "))
    block = []
    for x in lines[start:]:
        if not x:
            break
        block.append(x)
    assert len(block) > 1 and all(len(x) <= 40 for x in block)
    assert block[0][9:] + "".join(x[9:] for x in block[1:]) == flow.command_text()


def test_g_cycles_personal_work_none_and_w_changes_the_command(fake_env, index):
    flow = _flow(fake_env, index)
    flow.start("x y")
    _to_new_project(flow, fake_env.root / "home" / "ai")
    assert flow.github == "personal"
    flow.cycle_github()
    assert flow.github == "work"
    flow.cycle_github()
    assert flow.github == "none"
    assert "github   none" in _text(flow)
    flow.cycle_github()
    assert flow.github == "personal"
    flow.cycle_wrapper()
    assert "wrapper  claude6" in _text(flow) and ' ; claude6 -n "x y"' in _text(flow)


def test_github_none_creates_no_repo_and_personal_does(fake_env, index):
    runner = FakeRunner()
    flow = _flow(fake_env, index, runner=runner)
    flow.start("a b")
    _to_new_project(flow, fake_env.root / "home" / "ai", "plain")
    flow.choose_github("none")
    flow.enter()
    assert flow.step == "done" and (fake_env.root / "home" / "ai" / "plain" / "plan.md").is_file()
    assert ["git", "init", "-b", "main"] in runner.calls and not any(c[0] == "gh" for c in runner.calls)

    runner2 = FakeRunner({"gh repo create": (0, "", "")})
    flow2 = _flow(fake_env, index, runner=runner2)
    flow2.start("a b")
    _to_new_project(flow2, fake_env.root / "home" / "ai", "hosted")
    assert flow2.github == "none"  # remembered for the home bucket by the launch above
    flow2.choose_github("personal")
    flow2.enter()
    assert any(c[:3] == ["gh", "repo", "create"] for c in runner2.calls)


def test_unknown_values_are_rejected(fake_env, index):
    flow = _flow(fake_env, index)
    flow.start("x y")
    _go(flow, fake_env.root / "home")
    _pick(flow, "start")
    with pytest.raises(ValueError):
        flow.choose_wrapper("bash")
    with pytest.raises(ValueError):
        flow.choose_github("gitlab")


def test_work_wf_defaults_to_no_github_and_launch_remembers_the_buckets_choices(fake_env, index):
    r = fake_env.root
    (r / "work" / "wf").mkdir()
    flow = _flow(fake_env, index)
    flow.start("mediko export")
    _to_new_project(flow, r / "work" / "wf", "mediko_export")
    assert flow.github == "none"  # a workflow-export folder gets no repo unless asked
    flow.choose_wrapper("claudeb")
    flow.choose_github("work")
    flow.enter()
    saved = json.loads((fake_env.data_dir / "defaults.json").read_text(encoding="utf-8"))
    assert saved == {"work\\wf": {"wrapper": "claudeb", "github": "work"}}
    again = _flow(fake_env, index)
    again.start("second")
    _to_new_project(again, r / "work" / "wf", "second_export")
    assert (again.wrapper, again.github) == ("claudeb", "work")
    other = _flow(fake_env, index)  # another bucket is untouched
    other.start("third")
    _go(other, r / "work" / "net")
    _pick(other, "start")
    assert other.wrapper == "clauded"


def test_start_here_remembers_the_wrapper_but_keeps_the_github_choice(fake_env, index):
    from launcher import defaults
    r = fake_env.root
    defaults.remember(fake_env, r / "home", "clauded", "work")
    flow = _flow(fake_env, index)
    flow.start("x y")
    _go(flow, r / "home" / "ai")
    _pick(flow, "start")
    flow.choose_wrapper("claude6")
    flow.enter()
    assert defaults.get(fake_env, r / "home") == defaults.Defaults("claude6", "work")


def test_esc_on_the_confirm_step_goes_back_to_kind_for_a_new_project(fake_env, index):
    flow = _flow(fake_env, index)
    flow.start("x y")
    _to_new_project(flow, fake_env.root / "home" / "ai")
    flow.esc()
    assert flow.step == "kind" and flow.options()[flow.selected][0] == "project"
    flow.enter()
    assert flow.step == "confirm" and (flow.wrapper, flow.github) == ("clauded", "personal")


def test_creating_names_the_folder_only_for_a_new_project(fake_env, index):
    flow = _flow(fake_env, index)
    flow.start("x y")
    _to_new_project(flow, fake_env.root / "home" / "ai", "fresh")
    assert flow.creating() == fake_env.root / "home" / "ai" / "fresh"
    flow.esc()
    flow.esc()
    flow.esc()
    _go(flow, fake_env.root / "home" / "ai")
    _pick(flow, "start")
    assert flow.step == "confirm" and flow.creating() is None


def test_remembering_failure_is_a_warning_not_a_failed_launch(fake_env, index, monkeypatch):
    from launcher import defaults

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(defaults, "remember", boom)
    flow = _flow(fake_env, index)
    flow.start("x y")
    _go(flow, fake_env.root / "home")
    _pick(flow, "start")
    flow.enter()
    assert flow.step == "done" and any("could not remember" in w for w in flow.result().warnings)


def test_confirm_keys_in_the_app_w_g_cycle_q_does_not_cancel_enter_launches(fake_env, index):
    flow = _flow(fake_env, index)
    flow.start("x y")
    _to_new_project(flow, fake_env.root / "home" / "ai", "keys")
    launch = _run_keys(flow, "wwgqQ\r")
    assert isinstance(launch, Launch) and launch.argv[0] == "claudeb"
    assert flow.github == "work"  # personal, then g: work (two w: claude6, claudeb)


def test_ctrl_c_cancels_from_the_confirm_step(fake_env, index):
    flow = _flow(fake_env, index)
    flow.start("x y")
    _go(flow, fake_env.root / "home")
    _pick(flow, "start")
    assert _run_keys(flow, "w\x03") is None
    assert flow.step == "confirm"
    assert [e["event"] for e in _flowlog(fake_env)].count("cancel") == 1
