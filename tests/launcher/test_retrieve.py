from launcher.index import FolderStats
from launcher.retrieve import (Cand, Scored, candidates, idf_table, places, rank, stem_match, strong, tokens)
from launcher_helpers import make_row

DAY = 86400.0


def _rels(scored):
    return [s.cand.rel for s in scored]


# ---- tokeniser ------------------------------------------------------------------------------------------------------

def test_tokens_lower_split_length_and_stop_words():
    assert tokens("Ninja RMM Alert Review") == ["ninja", "rmm", "alert"]
    assert tokens("fix the new VPN-cert_issue  for ai") == ["vpn", "cert"]  # lower-case 'ai' too short, stop words
    assert tokens("for AI") == ["ai"]  # a capitalised 2-letter acronym is kept (see test_short_acronyms)
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


# ---- fix round ------------------------------------------------------------------------------------------------------

def test_claude_md_note_ignores_a_bom(fake_env):
    r = fake_env.root
    (r / "work" / "bommed").mkdir()
    (r / "work" / "bommed" / "CLAUDE.md").write_bytes("\ufeff# Bommed bucket\n\nWhat lives here.\n".encode("utf-8"))
    got = {p.rel: p for p in places(fake_env, candidates(fake_env, None, []), "x")}
    assert got["work\\bommed"].note == "Bommed bucket: What lives here."


def test_client_name_does_not_crowd_the_shortlist(fake_env):
    """Every folder of a client carries the client's name in its path; that word must not score them, or one
    client's folders fill the shortlist (seen on the real tree for "Bast monthly patch report")."""
    r, now = fake_env.root, fake_env.now()
    bast = r / "work" / "clients" / "bast"
    for name in [f"proj{i:02d}" for i in range(20)] + ["patching"]:
        (bast / name).mkdir()
    (r / "work" / "ninja" / "patch_reports").mkdir(parents=True)
    rows = [make_row(r / "work" / "ninja" / "patch_reports", "monthly patch summary", now - DAY, "s1")]
    cands = candidates(fake_env, None, rows)
    by_rel = {c.rel: c for c in cands}
    assert "bast" not in by_rel["work\\clients\\bast\\proj00"].tokens
    assert "bast" in by_rel["work\\clients\\bast"].tokens  # the client itself keeps its own name
    rels = _rels(rank("Bast monthly patch report", cands, now))
    assert rels[0] == "work\\ninja\\patch_reports"
    assert not any(x.startswith("work\\clients\\bast\\proj") for x in rels)
    assert "work\\clients\\bast\\patching" in rels  # its own name still matches
    assert "work\\clients\\bast" in {p.rel for p in places(fake_env, cands, "Bast monthly patch report")}


def test_places_carry_their_own_sessions(fake_env):
    r, now = fake_env.root, fake_env.now()
    (r / "home" / "ai" / "sub").mkdir()
    rows = [make_row(r / "home" / "ai", "whisper test", now - 60, "s1"),
            make_row(r / "home" / "ai", "ollama bench", now - 30, "s2"),
            make_row(r / "home" / "ai", "older one", now - 90, "s3"),
            make_row(r / "home" / "ai" / "sub", "below, not here", now, "s4")]
    got = {p.rel: p for p in places(fake_env, candidates(fake_env, None, rows), "x", rows)}
    assert got["home\\ai"].sessions_here == 3 and got["home\\ai"].names_here == ("ollama bench", "whisper test")
    assert got["home"].sessions_here == 0 and got["home"].names_here == ()


def test_folder_walk_is_cached_for_a_minute_and_dropped_on_create(fake_env, monkeypatch):
    import launcher.retrieve as retrieve
    from launcher.scaffold import create_folder

    calls = []
    real = retrieve._walk_tree
    monkeypatch.setattr(retrieve, "_walk_tree", lambda root: calls.append(root) or real(root))
    clock = [1000.0]
    monkeypatch.setattr(retrieve.time, "monotonic", lambda: clock[0])
    candidates(fake_env, None, [])
    candidates(fake_env, None, [])
    assert len(calls) == 1
    clock[0] += retrieve.WALK_TTL_S - 1
    candidates(fake_env, None, [])
    assert len(calls) == 1
    clock[0] += 2
    candidates(fake_env, None, [])
    assert len(calls) == 2  # stale after 60 s

    class NoRun:
        def run(self, *a, **k):
            raise AssertionError("no commands for a container")

    create_folder(fake_env.root / "home", "fresh", "container", "x", runner=NoRun())
    rels = [c.rel for c in candidates(fake_env, None, [])]
    assert len(calls) == 3 and "home\\fresh" in rels  # create_folder dropped the cache


# ---- round 3 --------------------------------------------------------------------------------------------------------

def test_own_folder_name_words_survive_the_organisational_exclusion(fake_env):
    r, now = fake_env.root, fake_env.now()
    for rel in ("work/ninja/ninja_alerts", "work/ninja/other_tool", "work/halo/halo_reports",
                "work/halo/views/sub"):
        (r / rel).mkdir(parents=True)
    rows = [make_row(r / "work" / "ninja" / "other_tool", "ninja thing", now - DAY, "s1"),
            make_row(r / "work" / "halo" / "views" / "sub", "halo sub work", now - DAY, "s2")]
    by_rel = {c.rel: c for c in candidates(fake_env, None, rows)}
    assert "ninja" in by_rel["work\\ninja\\ninja_alerts"].tokens  # its own name says ninja
    assert "halo" in by_rel["work\\halo\\halo_reports"].tokens
    assert "ninja" not in by_rel["work\\ninja\\other_tool"].tokens  # only a session name above it says ninja
    assert "halo" not in by_rel["work\\halo\\views\\sub"].tokens  # nor from the path above the leaf
    rels = _rels(rank("ninja alerts", candidates(fake_env, None, rows), now))
    assert rels[0] == "work\\ninja\\ninja_alerts"


def test_short_acronyms():
    assert tokens("CA policy for AD and VM, HR, IT") == ["ca", "policy", "ad", "vm", "hr", "it"]
    assert tokens("ca policy, ad hoc") == ["policy", "hoc"]  # lower-case 2-letter words are still dropped
    assert tokens("Ca") == [] and tokens("C2") == []  # capitals only, letters only
    assert tokens("CA_Policy Script") == ["ca", "policy"]
    assert not stem_match("ca", "cache") and not stem_match("ca", "calendar") and stem_match("ca", "ca")
    now = 1_700_000_000.0
    cands = [_cand("work\\m365\\CA Policy Script", ["CA rollout"], now - DAY),
             _cand("work\\m365\\calendar_cache", ["cache stuff"], now - DAY),
             _cand("home\\x")]
    assert _rels(rank("CA report", cands, now)) == ["work\\m365\\CA Policy Script"]


def test_empty_export_folders_do_not_fill_the_shortlist():
    """A workflow family: one parent folder with sessions and many dated, empty export sub-folders, plus other
    projects with sessions that share the name's words."""
    now = 1_700_000_000.0
    family = _cand("work\\wf\\acme_user_creation", ["acme onboarding"], now - DAY)
    exports = [_cand(f"work\\wf\\acme_user_creation\\Acme_New_Hire_Workflow_2026-0{m}-01", sessions=0)
               for m in range(1, 10)]
    elsewhere = [_cand(f"work\\wf\\Acme_Hire_Report_{i}", sessions=0) for i in range(6)]
    used = [_cand(f"work\\wf\\acme_task_{i}", [f"acme hire task {i}"], now - DAY) for i in range(16)]
    scored = rank("Acme new hire workflow", [family, *exports, *elsewhere, *used], now)
    rels = _rels(scored)
    assert len(rels) == 14
    assert not any("_2026-0" in x for x in rels)  # the exports folded into the family folder
    assert "work\\wf\\acme_user_creation" in rels
    fam = next(s for s in scored if s.cand.rel == "work\\wf\\acme_user_creation")
    assert fam.folded and fam.matched == ("acme",)  # the exports' score, but only its own words
    assert fam.score > sum(fam.weights) + 0.15  # more than its own words earn: the best export's score
    assert not any(x.startswith("work\\wf\\Acme_Hire_Report") for x in rels)  # empty, weaker, slots are full
    few = rank("Acme new hire workflow", [*elsewhere, used[0]], now)
    assert len(few) == 7  # fewer than k folders with sessions: the empty ones fill the slots


def test_a_folder_scored_by_a_folded_empty_sub_folder_is_never_strong():
    now = 1_700_000_000.0
    container = _cand("cc\\improvements", ["pxe notes"], now - DAY)  # scores on one word of its own
    empty = _cand("cc\\improvements\\pxe_boot_tool", sessions=0)
    others = [_cand(f"home\\filler{i}") for i in range(30)]
    scored = rank("pxe boot tool", [container, empty, *others], now)
    assert _rels(scored) == ["cc\\improvements"] and scored[0].folded
    assert not strong(scored)


def _named_fixture(named, n_filler=12, n_used=6):
    """`n_used` folders with sessions that score above every empty folder, `n_filler` empty folders that match two
    of the name's words in their path (not their own name) and sort before the `named` ones on a tie."""
    now = 1_700_000_000.0
    used = [_cand(f"work\\m365\\notes_{i}", ["teams migration channel mapping"], now - DAY) for i in range(n_used)]
    filler = [_cand(f"work\\a_teams_migration\\i{i}", sessions=0) for i in range(n_filler)]
    return now, used, filler, named


def test_an_empty_folder_named_for_the_session_takes_a_slot_other_empty_ones_would_have_had():
    named = _cand("work\\z\\teams_migration", sessions=0)
    now, used, filler, _ = _named_fixture(named)
    rels = _rels(rank("teams migration channel mapping", [*used, *filler, named], now))
    assert len(rels) == 14 and "work\\z\\teams_migration" in rels
    assert sum(1 for x in rels if x.startswith("work\\a_teams_migration")) == 7  # one filler made room


def test_a_named_empty_folder_never_evicts_a_folder_with_sessions():
    now = 1_700_000_000.0
    used = [_cand(f"work\\m365\\notes_{i}", ["teams migration channel mapping"], now - DAY) for i in range(16)]
    named = _cand("work\\z\\teams_migration", sessions=0)
    rels = _rels(rank("teams migration channel mapping", [*used, named], now))
    assert len(rels) == 14 and "work\\z\\teams_migration" not in rels
    assert all("notes_" in x for x in rels)


def test_two_different_empty_folders_named_for_the_session_are_both_kept():
    a = _cand("work\\z\\teams_migration", sessions=0)
    b = _cand("work\\y\\migration_teams", sessions=0)
    now, used, filler, _ = _named_fixture(None)
    rels = _rels(rank("teams migration channel mapping", [*used, *filler, a, b], now))
    assert "work\\z\\teams_migration" in rels and "work\\y\\migration_teams" in rels and len(rels) == 14


def test_three_empty_folders_named_for_the_session_keep_two_by_leaf_hits_score_recency():
    now = 1_700_000_000.0
    three = _cand("work\\x\\teams_migration_channel", sessions=0)  # 3 leaf hits: first
    recent = _cand("work\\z\\teams_migration", sessions=0, last=now - DAY)  # 2 hits, newer than `plain`
    plain = _cand("work\\y\\migration_teams", sessions=0)
    _now, used, filler, _ = _named_fixture(None)
    rels = _rels(rank("teams migration channel mapping", [*used, *filler, plain, recent, three], now))
    assert "work\\x\\teams_migration_channel" in rels and "work\\z\\teams_migration" in rels
    assert "work\\y\\migration_teams" not in rels and len(rels) == 14


def test_dated_export_siblings_collapse_to_the_newest_and_do_not_crowd_out_used_folders():
    now = 1_700_000_000.0
    exports = [_cand(f"work\\wf\\Rewst_Master_v3_Client_Offboarding_2026-0{m}-01", sessions=0) for m in range(1, 7)]
    hashed = [_cand("work\\wf\\Client_Offboarding_report_" + h, sessions=0) for h in ("3fa9c1d2", "9b7e4a10")]
    used = [_cand(f"work\\wf\\acme_{i}", ["client offboarding"], now - DAY) for i in range(3)]
    rels = _rels(rank("client offboarding fix", [*exports, *hashed, *used], now))
    assert {f"work\\wf\\acme_{i}" for i in range(3)} <= set(rels)
    dated = [x for x in rels if "Rewst_Master" in x]
    assert dated == ["work\\wf\\Rewst_Master_v3_Client_Offboarding_2026-06-01"]  # the newest by the date in the name
    assert sum(1 for x in rels if "Client_Offboarding_report" in x) == 1  # one hash group, one entry
    assert len([x for x in rels if x not in {f"work\\wf\\acme_{i}" for i in range(3)}]) <= 2


def test_dated_siblings_that_have_sessions_are_never_collapsed():
    now = 1_700_000_000.0
    cands = [_cand(f"work\\wf\\Export_2026-0{m}-01", [f"export run {m}"], now - m * DAY) for m in range(1, 4)]
    cands.append(_cand("work\\wf\\Export_2026-09-01", sessions=0))
    rels = _rels(rank("export run", cands, now))
    assert len([x for x in rels if "Export_2026-0" in x]) == 4  # the empty one is its own group: nothing to merge
