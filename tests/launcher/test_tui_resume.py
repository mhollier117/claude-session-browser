import json
from datetime import datetime
from pathlib import Path

import pytest

from launcher import cli
from launcher.index import Index, Row
from launcher.launch import Launch, resume_command
from launcher.tui.preview import render_preview
from launcher.tui.resume import (FOCUSED, KEYS, NEW, Resume, ResumeState, chrome_rows, footer_lines, render_rows,
                                 scroll_top)
from launcher_helpers import write_claude_session, write_qwen_session

NOW = 1_700_000_000.0
WIDTH = 100


def make_row(**kw) -> Row:
    base = dict(store="claude", sid="s1", cwd=None, name=None, display_name="x", first_ask="", last_ask="",
                last_reply="", user_msgs=1, assistant_msgs=1, size=1, mtime=NOW, last_activity=NOW,
                live_pid=None, wrapper=None, cwd_exists=True)
    base.update(kw)
    return Row(**base)


def _lines(frags) -> list[str]:
    return "".join(text for _, text in frags).split("\n")


def _u(text, cwd=None):
    rec = {"type": "user", "message": {"role": "user", "content": text}}
    if cwd is not None:
        rec["cwd"] = str(cwd)
    return rec


def _a(text):
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def _index(env):
    return Index(env, claude_live=lambda: [], pid_exists=lambda pid: False)


# ---- render_rows ---------------------------------------------------------------------------------------------

def test_render_rows_columns_and_ellipsis(fake_env):
    deep = fake_env.root / "work" / "clients" / "bast" / "bifrost" / "a_rather_long_folder" / "stackjack_rip_qwen"
    rows = [make_row(cwd=str(fake_env.root / "home" / "homelab"), display_name="homelab pxe boot order",
                     last_activity=NOW - 86400),
            make_row(sid="s2", store="qwen", cwd=str(deep), display_name="n" * 40, last_activity=NOW - 3 * 86400)]
    lines = _lines(render_rows(rows, WIDTH, NOW, 0, root=fake_env.root))
    header, rule, first, second = lines[:4]
    # header: when / session / location / tool aligned on the column starts (4, 28, fill, 7, 5 with 1-cell gaps)
    loc_w = WIDTH - (4 + 28 + 7 + 5 + 4)
    assert header.startswith("when session")
    assert header.index("session") == 5 and header.index("location") == 34
    assert header.index("tool") == 34 + loc_w + 1
    assert set(rule) == {"─"}
    for line in (first, second):
        assert len(line) == WIDTH
    # selected row is marked, location is relative to the root
    assert first.startswith("1d   ▶ homelab pxe boot order")
    assert first[34:34 + loc_w].rstrip() == r"home\homelab"
    assert first[34 + loc_w + 1:].rstrip() == "claude"
    # long name cut to 28 cells with an ellipsis; long location keeps first and last two segments
    assert second[5:33] == "n" * 27 + "…"
    assert second[34:34 + loc_w].rstrip() == r"work\…\a_rather_long_folder\stackjack_rip_qwen"
    assert second[34 + loc_w + 1:].rstrip() == "qwen"
    assert KEYS == ("↑↓ move · type to filter · tab preview · enter resume · ctrl-n new · "
                    "ctrl-o folder · ctrl-w wrapper · ctrl-q quit")
    assert lines[-2:] == footer_lines(WIDTH) == [
        "↑↓ move · type to filter · tab preview · enter resume · ctrl-n new · ctrl-o folder · ctrl-w wrapper",
        "ctrl-q quit"]  # wrapped between items, as the picker footer is


def test_render_rows_middle_ellipsis_on_narrow_terminal(fake_env):
    deep = fake_env.root / "work" / "clients" / "bast" / "bifrost" / "x" / "y" / "stackjack_rip_qwen"
    lines = _lines(render_rows([make_row(cwd=str(deep))], 80, NOW, 0, root=fake_env.root))
    loc_w = 80 - 48
    assert lines[2][34:34 + loc_w].rstrip() == r"work\…\y\stackjack_rip_qwen"


def test_render_rows_live_marker_and_order(fake_env):
    rows = [make_row(sid="a", display_name="live one", live_pid=42),
            make_row(sid="b", display_name="idle one", last_activity=NOW - 7200)]
    frags = render_rows(rows, WIDTH, NOW, 1, root=fake_env.root)
    lines = _lines(frags)
    assert lines[2].endswith("live ")  # live column, 5 cells
    assert lines[2][5:].startswith("live one")  # not selected: no marker
    assert lines[3].startswith("2h   ▶ idle one")
    assert lines[3].rstrip().endswith("claude")
    assert ("class:live", "live ") in frags
    # the selected row carries the selection style; rows keep the order they were given in
    assert any(style.startswith("class:sel") and "idle one" in text for style, text in frags)
    assert lines.index(lines[2]) < lines.index(lines[3])


def test_render_rows_window_and_status(fake_env):
    rows = [make_row(sid=str(i), display_name=f"row {i}") for i in range(50)]
    lines = _lines(render_rows(rows, WIDTH, NOW, 30, root=fake_env.root, top=25, height=12,
                               status="filter: ro"))
    body = [l for l in lines if l.startswith("now")]
    assert len(body) == 12 - 6  # header, rule, rule, two key lines, status
    assert chrome_rows(WIDTH, True) == 6
    assert "row 25" in body[0] and "▶ row 30" in body[5]
    assert lines[-1] == "filter: ro"


@pytest.mark.parametrize("width, lines", [(120, 1), (100, 2), (60, 2), (40, 4)])
def test_footer_wraps_between_items_and_loses_none(width, lines):
    from prompt_toolkit.utils import get_cwidth
    footer = footer_lines(width)
    assert len(footer) == lines and all(get_cwidth(x) <= width for x in footer)
    assert " · ".join(footer) == KEYS  # every item survives, in order, whole
    assert chrome_rows(width) == 3 + lines


@pytest.mark.parametrize("width", [120, 100, 60, 40])
def test_footer_does_not_push_the_list_off_the_screen(fake_env, width):
    rows = [make_row(sid=str(i), display_name=f"row {i}") for i in range(50)]
    for height in (12, 20):
        for status in ("", "filter: ro"):
            lines = _lines(render_rows(rows, width, NOW, 49, root=fake_env.root, top=0, height=height, status=status))
            assert len(lines) == height  # header, rules, footer lines, status and the visible rows fill the screen
            assert "▶ row 49" in "".join(lines)  # the selection stays visible whatever the footer takes


def test_render_rows_empty_list(fake_env):
    lines = _lines(render_rows([], WIDTH, NOW, 0, root=fake_env.root))
    assert "no sessions" in lines[2]


def test_scroll_top_keeps_selection_visible():
    assert scroll_top(0, 3, 10) == 0
    assert scroll_top(0, 12, 10) == 3
    assert scroll_top(5, 2, 10) == 2
    assert scroll_top(5, 9, 10) == 5


# ---- render_preview ------------------------------------------------------------------------------------------

def test_render_preview_contains_three_sections_and_wraps(fake_env):
    cwd = fake_env.root / "home" / "homelab"
    reply = ("word " * 60).strip() + "\n\n1. first step\n2. second step"
    row = make_row(cwd=str(cwd), display_name="homelab pxe boot order", first_ask="set up PXE " * 20,
                   last_ask="why does the second NIC fail?", user_msgs=90, assistant_msgs=94,
                   size=48 * 1024 * 1024, last_activity=NOW - 3600)
    launch = resume_command(row, None, root=fake_env.root)
    text = render_preview(row, reply, 60, launch=launch)
    lines = text.split("\n")
    stamp = datetime.fromtimestamp(NOW - 3600).strftime("%Y-%m-%d %H:%M")
    assert lines[0].startswith("homelab pxe boot order")
    assert f"claude · {stamp} · 184 messages · 48 MB" in text
    assert "".join(lines[2:lines.index("first ask") - 1]) == str(cwd)  # full path, wrapped when long
    for section in ("first ask", "last ask", "last reply"):
        assert section in lines
    assert all(len(line) <= 58 for line in lines), [l for l in lines if len(l) > 58]
    body = lines[lines.index("last reply") + 1:]
    assert body[0].startswith("  word word")
    assert "  1. first step" in body and "  2. second step" in body
    assert "" in body[:body.index("  1. first step")]  # paragraph break kept
    assert "  clauded -r s1" in lines
    assert lines[-1] == "space/esc back · ↑↓ scroll · enter resume · o open folder"


def test_render_preview_live_and_missing(fake_env):
    row = make_row(cwd=r"C:\gone\away", cwd_exists=False, live_pid=77, first_ask="", last_ask="")
    text = render_preview(row, "", 80)
    assert "folder no longer exists" in text
    assert "(none)" in text
    assert "pid 77" in text


# ---- ResumeState (key handling without a terminal) -----------------------------------------------------------

def _two_sessions(env):
    a = env.root / "home" / "homelab"
    b = env.root / "work" / "net"
    p = write_claude_session(env, a, "aaa", [_u("pxe boot", a), _a("ok")])
    q = write_qwen_session(env, b, "qqq", [{"type": "user", "provenance": "real_user",
                                            "message": {"role": "user", "parts": [{"text": "fortigate vpn"}]}},
                                           {"type": "assistant", "message": {"role": "model",
                                                                             "parts": [{"text": "done"}]}}])
    import os
    os.utime(p, (NOW, NOW))
    os.utime(q, (NOW - 600, NOW - 600))
    idx = _index(env)
    idx.refresh()
    return idx


def test_state_filter_move_and_enter(fake_env):
    idx = _two_sessions(fake_env)
    st = ResumeState(fake_env, idx)
    assert [r.sid for r in st.rows] == ["aaa", "qqq"]
    st.move(1)
    assert st.current().sid == "qqq"
    st.set_filter("pxe")
    assert [r.sid for r in st.rows] == ["aaa"] and st.selected == 0
    result = st.enter()
    assert isinstance(result, Resume)
    assert result.launch.argv == ["clauded", "-r", "aaa"] and result.wrapper == "clauded"
    assert result.row.path.endswith("aaa.jsonl")


def test_state_every_printable_key_filters_including_command_letters_and_inner_spaces(fake_env):
    st = ResumeState(fake_env, _two_sessions(fake_env))
    assert [st.type_char(c) for c in "nowq"] == [None] * 4  # no longer commands
    assert st.filter == "nowq" and st.footer_status() == "filter: nowq▏" and st.rows == []
    for _ in range(4):
        st.backspace()
    assert st.filter == "" and len(st.rows) == 2 and st.footer_status() == ""
    for c in "pxe boot":  # a space inside a filter is text
        assert st.type_char(c) is None
    assert st.filter == "pxe boot" and [r.sid for r in st.rows] == ["aaa"]
    assert st.type_char("/") is None and st.filter == "pxe boot/"  # no special slash rule any more


def test_state_space_opens_the_preview_only_while_the_filter_is_empty(fake_env):
    st = ResumeState(fake_env, _two_sessions(fake_env))
    assert st.type_char(" ") == "preview" and st.filter == ""  # not a leading space in the filter
    st.type_char("p")
    assert st.type_char(" ") is None and st.filter == "p "


def test_state_escape_clears_the_filter_and_does_nothing_when_empty(fake_env):
    st = ResumeState(fake_env, _two_sessions(fake_env))
    assert st.escape() is False and st.filter == "" and len(st.rows) == 2
    st.type_char("f")
    assert st.escape() is True and st.filter == "" and len(st.rows) == 2
    assert st.escape() is False


def test_state_chords(fake_env):
    opened = []
    st = ResumeState(fake_env, _two_sessions(fake_env), popen=lambda argv: opened.append(argv))
    assert st.chord("c-n") == "new" and st.chord("c-q") == "quit" and st.chord("c-c") == "quit"
    assert st.chord("n") is None and st.chord("q") is None and st.filter == ""  # letters are not chords
    assert st.chord("c-w") is None and st.wrapper_for(st.current()) == "claude6"
    assert st.chord("c-o") is None and opened == [["explorer.exe", str(fake_env.root / "home" / "homelab")]]
    st.type_char("p")
    st.chord("c-w")  # chords work with a filter typed, and leave it alone
    assert st.filter == "p" and st.wrapper_for(st.current()) == "claudeb"


def _run_keys(fake_env, keys):
    """Run the real prompt_toolkit resume app on a pipe, feeding `keys`; returns its result."""
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from launcher.tui.app import build_app
    idx = _two_sessions(fake_env)
    with create_pipe_input() as pipe:
        app = build_app(fake_env, idx, output=DummyOutput(), input=pipe)
        pipe.send_text(keys)
        return app.run()


def test_app_chords_and_typing_through_the_real_key_bindings(fake_env):
    assert _run_keys(fake_env, "\x0e") is NEW  # ctrl-n
    assert _run_keys(fake_env, "\x11") is None  # ctrl-q
    assert _run_keys(fake_env, "\x03") is None  # ctrl-c
    assert _run_keys(fake_env, "nowq\x11") is None  # letters filter, they do not quit or start anything
    res = _run_keys(fake_env, "pxe boot\r")  # letters and an inner space filter; enter resumes the only match
    assert isinstance(res, Resume) and res.row.sid == "aaa"
    res = _run_keys(fake_env, "\x17\r")  # ctrl-w cycles the wrapper of the selected row
    assert res.launch.argv == ["claude6", "-r", "aaa"]
    res = _run_keys(fake_env, "\t\r")  # tab opens the preview; enter there resumes
    assert isinstance(res, Resume) and res.row.sid == "aaa"
    res = _run_keys(fake_env, " \r")  # space on an empty filter opens the preview too
    assert isinstance(res, Resume) and res.row.sid == "aaa"


def test_state_cycle_wrapper(fake_env):
    idx = _two_sessions(fake_env)
    st = ResumeState(fake_env, idx)
    st.cycle_wrapper()
    assert st.wrapper_for(st.current()) == "claude6" and "claude6" in st.status
    for _ in range(4):
        st.cycle_wrapper()  # claudeb, claudew, then wraps past qwen to claude, clauded
    assert st.wrapper_for(st.current()) == "clauded"
    assert st.enter().launch.argv[0] == "clauded"
    st.move(1)
    st.cycle_wrapper()
    assert st.wrapper_for(st.current()) == "qwen" and "qwen" in st.status


def test_state_live_row_focuses(fake_env):
    calls = []
    st = ResumeState(fake_env, _two_sessions(fake_env), focus=lambda row: calls.append(row) or {"ok": True})
    st.rows[0].live_pid = 99
    assert st.enter() is FOCUSED and calls
    st2 = ResumeState(fake_env, _two_sessions(fake_env),
                      focus=lambda row: {"ok": False, "error": "no window"})
    st2.rows[0].live_pid = 99
    assert st2.enter() is None and "no window" in st2.status


def test_state_open_folder(fake_env):
    opened = []
    st = ResumeState(fake_env, _two_sessions(fake_env), popen=lambda argv: opened.append(argv))
    st.open_folder()
    assert opened == [["explorer.exe", str(fake_env.root / "home" / "homelab")]]
    st.rows[0].cwd_exists = False
    st.open_folder()
    assert len(opened) == 1 and "no longer exists" in st.status


def test_state_preview_reads_full_reply(fake_env):
    st = ResumeState(fake_env, _two_sessions(fake_env))
    text = st.preview_text(80)
    assert "pxe boot" in text and "  ok" in text


# ---- cli ---------------------------------------------------------------------------------------------------

def test_cli_json_lists_rows(fake_env, capsys):
    idx = _two_sessions(fake_env)
    assert cli.main(["--json"], env=fake_env, index=idx) == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert [d["sid"] for d in data] == ["aaa", "qqq"]
    assert data[0]["store"] == "claude" and data[0]["cwd"].endswith("homelab") and "path" in data[0]


def test_cli_prints_single_json_line_on_choice(fake_env, capsys):
    idx = _two_sessions(fake_env)
    launch = Launch(Path(r"C:\proj"), ["clauded", "-r", "abc"], ["careful"])
    assert cli.main([], env=fake_env, index=idx, app=lambda env, index: launch) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    assert json.loads(out) == {"cd": r"C:\proj", "run": ["clauded", "-r", "abc"], "warnings": ["careful"]}


def test_cli_resume_records_wrapper(fake_env, capsys):
    idx = _two_sessions(fake_env)

    def app(env, index):
        st = ResumeState(env, index)
        st.cycle_wrapper()
        return st.enter()

    assert cli.main([], env=fake_env, index=idx, app=app) == 0
    assert json.loads(capsys.readouterr().out)["run"] == ["claude6", "-r", "aaa"]
    assert idx.row("claude", "aaa").wrapper == "claude6"


@pytest.mark.parametrize("result", [None, FOCUSED, NEW])
def test_cli_prints_nothing_without_launch(fake_env, capsys, result):
    idx = _two_sessions(fake_env)
    assert cli.main([], env=fake_env, index=idx, app=lambda env, index: result,
                    new_app=lambda env, index, name: None) == 0  # NEW: the new-session walk, quit at once
    assert capsys.readouterr().out == ""


def test_cli_index_reports_to_stderr(fake_env, capsys):
    idx = _index(fake_env)
    write_claude_session(fake_env, fake_env.root / "cc", "ccc", [_u("hello", fake_env.root / "cc"), _a("hi")])
    assert cli.main(["index"], env=fake_env, index=idx) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "1 session" in captured.err


@pytest.mark.parametrize("argv", [["index"], [], ["--json"], ["new", "x"]])
def test_cli_index_busy_exits_2_with_empty_stdout(fake_env, capsys, argv):
    import sqlite3
    idx = Index(fake_env, claude_live=lambda: [], pid_exists=lambda pid: False, timeout=0.05)
    assert idx._db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    write_claude_session(fake_env, fake_env.root / "cc", "ccc", [_u("hello", fake_env.root / "cc"), _a("hi")])
    other = sqlite3.connect(idx.db_path, timeout=0)  # another cc (or the browser) in the middle of its refresh
    other.execute("BEGIN IMMEDIATE")
    try:
        assert cli.main(argv, env=fake_env, index=idx, app=lambda e, i: pytest.fail("app ran"),
                        new_app=lambda e, i, n: pytest.fail("new app ran")) == 2
    finally:
        other.rollback()
        other.close()
        idx.close()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "index busy (another cc or the browser is indexing), try again in a moment" in captured.err


def test_cli_index_busy_while_opening_exits_2(fake_env, capsys, monkeypatch):
    import sqlite3
    import launcher.index as index_mod

    def locked(*a, **kw):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(index_mod.Index, "__init__", locked)
    assert cli.main(["index"], env=fake_env) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "index busy" in captured.err


def test_cli_index_busy_in_the_app_exits_2(fake_env, capsys):
    import sqlite3
    idx = _two_sessions(fake_env)

    def app(env, index):
        raise sqlite3.OperationalError("database is locked")  # e.g. record_launch while another cc refreshes
    assert cli.main([], env=fake_env, index=idx, app=app) == 2
    assert capsys.readouterr().out == ""


# ---- fix round 1---------------------------------------------------------------------------------------------

def test_render_preview_wraps_wide_text_by_cells(fake_env):
    from prompt_toolkit.utils import get_cwidth
    row = make_row(cwd=r"C:\x", display_name="漢字の名前" * 6, first_ask="日本語のテキスト " * 30,
                   last_ask="😀" * 80)
    text = render_preview(row, "長い" * 100, 40)
    lines = text.split("\n")
    body = lines[:-1]  # the fixed footer text is the one line allowed to be cut by a very narrow window
    assert all(get_cwidth(line) <= 38 for line in body), [l for l in body if get_cwidth(l) > 38]
    joined = "".join(l.strip() for l in lines)
    assert joined.count("😀") == 80 and joined.count("長い") == 100  # nothing lost at the line ends


def test_cli_does_not_record_launch_for_missing_folder(fake_env, capsys):
    idx = _two_sessions(fake_env)

    def app(env, index):
        st = ResumeState(env, index)
        st.rows[0].cwd_exists = False
        return st.enter()

    assert cli.main([], env=fake_env, index=idx, app=app) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["warnings"] and "no longer exists" in out["warnings"][0]
    assert idx.row("claude", "aaa").wrapper is None


@pytest.mark.parametrize("where", ["refresh", "app"])
def test_cli_ctrl_c_exits_130_quietly(fake_env, capsys, monkeypatch, where):
    idx = _two_sessions(fake_env)

    def boom(*a, **k):
        raise KeyboardInterrupt

    if where == "refresh":
        monkeypatch.setattr(idx, "refresh", boom)
    assert cli.main([], env=fake_env, index=idx, app=boom) == 130
    assert capsys.readouterr().out == ""


def test_spawn_detached_leaves_no_resource_warning():
    import gc
    import sys
    import time
    import warnings
    from launcher.tui.resume import spawn_detached
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        proc = spawn_detached([sys.executable, "-c", "pass"])
        deadline = time.time() + 10
        while proc.returncode is None and time.time() < deadline:
            time.sleep(0.05)  # the reaper thread sets returncode
        del proc
        gc.collect()
    assert not [w for w in caught if issubclass(w.category, ResourceWarning)]


def test_cli_resume_goes_on_when_the_index_is_locked(fake_env, capsys):
    """record_launch is best-effort: a locked index adds a warning and still prints the launch line."""
    import sqlite3
    import time
    idx = _two_sessions(fake_env)
    other = sqlite3.connect(idx.db_path, timeout=0)  # another cc in the middle of its refresh
    other.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        assert cli.main([], env=fake_env, index=idx, app=lambda e, i: ResumeState(e, i).enter()) == 0
        waited = time.monotonic() - started
    finally:
        other.rollback()
        other.close()
    out = json.loads(capsys.readouterr().out)
    assert out["run"] == ["clauded", "-r", "aaa"]
    assert any("could not remember the wrapper" in w for w in out["warnings"])
    assert waited < 5  # nowhere near the 30 s a refresh waits
    assert idx.row("claude", "aaa").wrapper is None
    idx.record_launch("claude", "aaa", "claude6")  # the connection is usable again once the lock is gone
    assert idx.row("claude", "aaa").wrapper == "claude6"
