from launcher.index import FolderStats, Index
from launcher.tree import children, folder_stats_map, level_kind, new_folder_kind_options, resolve_fragment, slug
from launcher_helpers import make_row, write_claude_session


def _u(text, cwd, ts="2026-11-01T10:00:00Z"):
    return {"type": "user", "cwd": str(cwd), "timestamp": ts, "message": {"role": "user", "content": text}}


def _index(env):
    return Index(env, claude_live=lambda: [])


def test_level_kinds(fake_env):
    r = fake_env.root
    assert level_kind(r, r) == "root"
    assert level_kind(r, r / "work") == "area" and level_kind(r, r / "home") == "area" and level_kind(r, r / "cc") == "area"
    assert level_kind(r, r / "work" / "net") == "bucket"
    assert level_kind(r, r / "work" / "clients" / "bast") == "client"
    assert level_kind(r, r / "cc" / "reorg") == "project"
    assert level_kind(r, r / "home" / "ai") == "project"
    assert level_kind(r, r / "work" / "net" / "pxe") == "project"
    assert level_kind(r, r / "WORK" / "Clients" / "bast") == "client"


def test_new_folder_kind_options(fake_env):
    r = fake_env.root
    assert new_folder_kind_options(r, r) == ["organisational"]
    assert new_folder_kind_options(r, r / "work") == ["organisational"]
    assert new_folder_kind_options(r, r / "WORK") == ["organisational"]
    assert new_folder_kind_options(r, r / "work" / "clients") == ["client"]
    assert new_folder_kind_options(r, r / "work" / "net") == ["project", "container"]
    assert new_folder_kind_options(r, r / "work" / "net" / "pxe") == ["project", "container"]
    assert new_folder_kind_options(r, r / "home") == ["project", "container"]
    assert new_folder_kind_options(r, r / "cc") == ["project", "container"]
    assert new_folder_kind_options(r, r / "work" / "clients" / "bast") == ["project", "container"]
    assert new_folder_kind_options(r, r / "work" / "clients" / "bast" / "bifrost") == ["project", "container"]


def test_resolve_fragment_cl_ba_bi(fake_env):
    r = fake_env.root
    (r / "work" / "clients" / "bast" / "bifrost_old").mkdir()
    (r / "work" / "clients" / "bast" / "other").mkdir()
    got = resolve_fragment(fake_env, r / "work", "cl/ba/bi")
    assert got == [r / "work" / "clients" / "bast" / "bifrost", r / "work" / "clients" / "bast" / "bifrost_old"]
    assert resolve_fragment(fake_env, r / "work", "CL/BA/zz") == []
    assert resolve_fragment(fake_env, r, "w/n") == [r / "work" / "net"]


def test_slug():
    assert slug("Stackjack rip, phase 2!") == "stackjack_rip_phase_2"
    assert slug("v1.2-Final") == "v1.2-final"
    assert slug("-x_") == "-x"


def test_children_counts_whole_components_case_insensitively(fake_env):
    r = fake_env.root
    (r / "work" / "network").mkdir()
    network, net_upper = r / "work" / "network", str(r) + r"\WORK\Net"
    write_claude_session(fake_env, network, "w1", [_u("network one", network)])
    write_claude_session(fake_env, r / "work" / "net", "w2", [_u("net upper", net_upper)])
    idx = _index(fake_env)
    idx.refresh()
    by = {c.name: c.stats for c in children(fake_env, idx, r / "work")}
    assert by["network"].sessions == 1 and by["network"].names == ["network one"]
    assert by["net"].sessions == 1 and by["net"].names == ["net upper"]


def test_children_filters_dirs_and_kinds(fake_env):
    r = fake_env.root
    for hidden in (".git", "_archive", "node_modules"):
        (r / "work" / hidden).mkdir()
    (r / "work" / "notes.txt").write_text("x")
    got = children(fake_env, _index(fake_env), r / "work")
    assert [c.name for c in got] == ["clients", "halo", "net", "vendors"]
    assert {c.kind for c in got} == {"bucket"}
    assert got[0].path == r / "work" / "clients"
    client = children(fake_env, _index(fake_env), r / "work" / "clients")[0]
    assert (client.name, client.kind) == ("bast", "client")


def test_children_stats_per_child_recursive(fake_env):
    r = fake_env.root
    bif, net = r / "work" / "clients" / "bast" / "bifrost", r / "work" / "net"
    write_claude_session(fake_env, bif, "s1", [_u("bifrost one", bif)])
    write_claude_session(fake_env, net, "s2", [_u("net one", net)])
    write_claude_session(fake_env, bif, "s3", [_u("bifrost two", bif)])
    idx = _index(fake_env)
    idx.refresh()
    by = {c.name: c.stats for c in children(fake_env, idx, r / "work")}
    assert by["clients"].sessions == 2 and by["net"].sessions == 1 and by["halo"].sessions == 0
    assert by["halo"].last_activity is None and by["halo"].names == []
    assert by["clients"].last_activity == max(x.last_activity for x in idx.rows() if "bifrost" in x.display_name)
    for name, stats in by.items():  # same semantics as Index.folder_stats
        assert stats == idx.folder_stats(f"work/{name}")


def test_folder_stats_map_one_pass(fake_env):
    r = fake_env.root
    rows = [make_row(r / "work" / "net", "net old", 10.0, "a"),
            make_row(str(r) + r"\WORK\Net\sub", "net sub", 30.0, "b"),
            make_row(r / "work" / "network", "network", 20.0, "c"),
            make_row(r / "work" / "net", "net mid", 25.0, "d"),
            make_row(r / "work" / "net", "net newest", 40.0, "e"),
            make_row(None, "no cwd", 50.0, "f")]
    m = folder_stats_map(rows)
    key = tuple(p.lower() for p in (r / "work" / "net").parts)
    assert m[key] == FolderStats(4, 40.0, ["net newest", "net sub", "net mid"])
    assert m[tuple(p.lower() for p in (r / "work").parts)].sessions == 5
    assert key + ("sub",) in m and tuple(p.lower() for p in (r / "work" / "halo").parts) not in m
