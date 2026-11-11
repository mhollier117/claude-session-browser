from launcher.index import FolderStats
from launcher.retrieve import (Cand, Scored, candidates, idf_table, places, rank, stem_match, strong, tokens)
from launcher_helpers import make_row

DAY = 86400.0


def _rels(scored):
    return [s.cand.rel for s in scored]


# ---- tokeniser ------------------------------------------------------------------------------------------------------

def test_tokens_lower_split_length_and_stop_words():
    assert tokens("Ninja RMM Alert Review") == ["ninja", "rmm", "alert"]
    assert tokens("fix the new VPN-cert_issue  for AI") == ["vpn", "cert"]  # 'ai' too short, stop words dropped
    assert tokens("Halo ticket list sync bug") == ["halo", "ticket", "sync", "bug"]
    assert tokens("") == [] and tokens(None) == []
    assert tokens("sync sync SYNC") == ["sync"]  # distinct, first-seen order


def test_stem_match():
    assert stem_match("vpn", "vpn")
    assert stem_match("standards", "standard")  # both >= 5 and share a 5-char prefix
    assert stem_match("decompile", "decompiler")
    assert not stem_match("rmm", "rmms")  # short tokens must be equal
    assert not stem_match("cert", "certificate")  # 'cert' is under 5 chars
    assert stem_match("alert", "alerts")
    assert not stem_match("halo", "hal")


# ---- candidates ------------------------------------------------------------------------------------------------------

def test_candidates_walk_depths_hidden_rule_and_tokens(fake_env):
    r, now = fake_env.root, fake_env.now()
    (r / "work" / "clients" / "bast" / "bifrost" / "a" / "too_deep").mkdir(parents=True)
    (r / "cc" / "reorg" / "pkg" / "too_deep").mkdir(parents=True)
    (r / "cc" / ".git").mkdir()
    (r / "cc" / "_old").mkdir()
    (r / "cc" / "node_modules").mkdir()
    (r / "other_area").mkdir()
    rows = [make_row(r / "work" / "net", "fortigate vpn tunnel", now - DAY, "s1")]
    cands = candidates(fake_env, None, rows)
    rels = [c.rel for c in cands]
    assert "work" in rels and "home" in rels and "cc" in rels
    assert "work\\clients\\bast\\bifrost\\a" in rels and not any("too_deep" in x for x in rels)
    assert "cc\\reorg\\pkg" in rels
    assert not any(x.endswith((".git", "_old", "node_modules")) or x == "other_area" for x in rels)
    net = next(c for c in cands if c.rel == "work\\net")
    assert net.depth == 2 and net.path == r / "work" / "net"
    assert net.stats.sessions == 1
    assert net.tokens == {"net", "fortigate", "vpn", "tunnel"}  # 'work' is a stop word


# ---- rank ------------------------------------------------------------------------------------------------------------

def _cand(rel, names=(), last=None, sessions=None, root=None):
    from pathlib import Path
    words = tokens(rel.replace("\\", " ") + " " + " ".join(names))
    return Cand(rel, Path("C:/root") / rel, rel.count("\\") + 1,
                FolderStats(len(names) if sessions is None else sessions, last, list(names)), frozenset(words))


def test_rare_token_folder_ranks_above_a_generic_token_folder():
    now = 1_700_000_000.0
    cands = [
        _cand("work"), _cand("work\\ninja"), _cand("work\\wf"), _cand("home"), _cand("cc"),
        _cand("work\\wf\\mediko_user_creation", ["Mediko alert review", "Mediko review alert flow"], now - DAY),
        _cand("work\\wf\\mediko_offboarding", ["Mediko alert offboarding"], now - DAY),
        _cand("work\\wf\\alert_router", ["alert routing"], now - 2 * DAY),
        _cand("work\\ninja\\rmm_policies", ["ninja rmm policy export"], now - 90 * DAY),
    ]
    scored = rank("Ninja RMM Alert Review", cands, now)
    assert scored[0].cand.rel == "work\\ninja\\rmm_policies"
    assert _rels(scored).index("work\\wf\\mediko_user_creation") > 0
    assert not {"work", "work\\ninja", "work\\wf", "home", "cc"} & set(_rels(scored))  # organisational: never ranked
    top = scored[0]
    assert set(top.matched) == {"ninja", "rmm"}


def test_rank_recency_bonus_and_k():
    now = 1_700_000_000.0
    cands = [_cand(f"home\\proj{i}", [f"widget thing {i}"], now - (i * 20) * DAY) for i in range(20)]
    scored = rank("widget", cands, now, k=5)
    assert len(scored) == 5
    assert scored[0].score > scored[-1].score  # the recent ones carry +0.15
    assert round(scored[0].score - scored[-1].score, 2) == 0.15
    assert rank("zzzz", cands, now) == []
    assert rank("", cands, now) == []


def test_idf_uses_stem_merged_document_frequency():
    cands = [_cand("home\\standards"), _cand("home\\standard_x"), _cand("home\\other")]
    idf = idf_table(["standard"], cands)
    # 'standard' stem-matches both 'standards' and 'standard': df = 2 of 3
    import math
    assert abs(idf["standard"] - math.log(4 / 3)) < 1e-9


# ---- strong ----------------------------------------------------------------------------------------------------------

def test_strong_needs_sessions_two_rare_tokens_and_a_clear_lead():
    a, b = _cand("home\\a", ["pxe boot"]), _cand("home\\b", ["pxe"])
    assert strong([Scored(a, 6.0, ("pxe", "boot"), (3.0, 3.0)), Scored(b, 3.0, ("pxe",), (3.0,))])
    assert not strong([Scored(a, 6.0, ("pxe", "boot"), (3.0, 3.0)), Scored(b, 5.0, ("pxe", "boot"), (3.0, 2.0))])
    assert not strong([Scored(a, 3.0, ("pxe",), (3.0,))])  # one token only
    assert not strong([Scored(a, 1.0, ("work", "home"), (0.5, 0.5))])  # common tokens only
    assert strong([Scored(a, 6.0, ("pxe", "boot"), (3.0, 3.0))])  # no runner-up
    empty = _cand("home\\pxe_boot")  # never worked in
    assert not strong([Scored(empty, 6.0, ("pxe", "boot"), (3.0, 3.0))])
    child = _cand("home\\a\\sub", ["x"])  # its own branch is no rival
    assert strong([Scored(a, 6.0, ("pxe", "boot"), (3.0, 3.0)), Scored(child, 5.5, ("pxe", "boot"), (3.0, 2.5))])
    assert not strong([])


def test_rank_keeps_one_folder_per_branch_match():
    now = 1_700_000_000.0
    cands = [_cand("work\\halo\\contact_sync", ["contact sync audit"], now - DAY),
             _cand("work\\halo\\contact_sync\\data\\out"),  # carries the path words only
             _cand("cc\\claude_improvements", ["claude session browser fix", "other", "x"], now - DAY),
             _cand("cc\\claude_improvements\\claude-session-browser-main", ["claude session browser"], now - DAY),
             _cand("home\\other")]
    rels = _rels(rank("contact sync duplicate", cands, now))
    assert rels == ["work\\halo\\contact_sync"]  # the project, not its data\out
    rels = _rels(rank("claude session browser launcher", cands, now))
    assert rels == ["cc\\claude_improvements\\claude-session-browser-main"]  # the project, not its container


def test_clients_and_home_places_are_never_projects():
    cands = [_cand("work\\clients\\Mediko", ["mediko x"]), _cand("home\\ai", ["ai transcription"]),
             _cand("home\\homelab", ["homelab pxe"]), _cand("work\\clients\\Mediko\\spokane", ["mediko drive"])]
    assert _rels(rank("mediko transcription homelab", cands, 0.0)) == ["work\\clients\\Mediko\\spokane"]


# ---- places ----------------------------------------------------------------------------------------------------------

def test_places_lists_areas_buckets_from_disk_and_reads_claude_md(fake_env):
    r = fake_env.root
    (r / "work" / "bifrost_automation").mkdir()
    (r / "work" / "bifrost_automation" / "CLAUDE.md").write_text(
        "# Bifrost automation\n\n  Bifrost tool code and its deploy scripts.  \nmore\n", encoding="utf-8")
    (r / "work" / "_archive").mkdir()
    (r / "work" / "clients" / "Mediko").mkdir()
    (r / "work" / "clients" / "acme").mkdir()
    cands = candidates(fake_env, None, [])
    got = {p.rel: p for p in places(fake_env, cands, "Mediko new hire workflow fix")}
    assert got["work\\bifrost_automation"].note == "Bifrost automation: Bifrost tool code and its deploy scripts."
    assert got["work\\bifrost_automation"].path == r / "work" / "bifrost_automation"
    assert {"work", "home", "cc", "work\\net", "work\\halo", "work\\vendors", "work\\clients"} <= set(got)
    assert got["work\\net"].note.startswith("Fortinet")  # built-in note
    assert got["home\\ai"].note and got["home\\homelab"].note  # known home places
    assert "work\\clients\\Mediko" in got and "work\\clients\\acme" not in got  # client named in the session
    assert "work\\_archive" not in got
    (r / "work" / "net" / "CLAUDE.md").write_text("# Net\nagent instructions\n", encoding="utf-8")
    (r / "work" / "zz").mkdir()
    (r / "work" / "zz" / "CLAUDE.md").write_text(
        "# CLAUDE.md — Long one\n\n- a list item\n| table |\n> quote\n" + "x" * 300 + "\n", encoding="utf-8")
    (r / "work" / "yy").mkdir()
    (r / "work" / "yy" / "CLAUDE.md").write_text("only a plain line\n", encoding="utf-8")
    got = {p.rel: p for p in places(fake_env, cands, "x")}
    assert got["work\\net"].note.startswith("Fortinet")  # a known place keeps its built-in note
    assert got["work\\zz"].note == ("Long one: " + "x" * 300)[:120]
    assert got["work\\yy"].note == "only a plain line"
