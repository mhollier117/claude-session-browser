"""Local retrieval for the suggester: which existing folders a session name is about, and where a new one could go.

`candidates` lists the folders the prompt used to list (depth 4 below work and home, 2 below cc, same hidden-folder
rule as the picker), each with the words of its relative path and its 3 most recent session names. `rank` scores
them by the IDF of the name's words they contain (stem-matched), so a rare word ("ninja", "pxe") outweighs one that
many folders share. `places` lists the areas and buckets (plus clients the name mentions) where a new folder could
be made, each with a one-line note. `strong` says whether the top result is clear enough to act on without a model.
Nothing here touches the network or the index database: rows come in as a list.
"""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .index import FolderStats
from .paths import Env
from .tree import _SKIP_NAMES, _listed, folder_stats_map

STOP = frozenset(
    "new fix phase update the and for with from into this that review check test issue script setup app tool "
    "project work session run add get set use data file files list build make".split())
DEPTH = {"work": 4, "home": 4, "cc": 2}  # levels listed below each area
RECENT_S = 30 * 86400.0
RECENT_BONUS = 0.15
# strong(): a token counts as rare when its IDF is at least RARE_IDF; the top result needs sessions, STRONG_TOKENS
# rare tokens, an IDF sum of at least STRONG_SUM, and LEAD times the runner-up's score. Calibrated with
# tools/suggest_eval.py on the real tree (2,538 folders): strong for 3 of the 15 names, all 3 right; none of the 8
# wrong top results was strong (without the sessions rule, the 0-session ninja_rmm_alert_renewal_review was).
RARE_IDF = 1.5
STRONG_TOKENS = 2
STRONG_SUM = 4.0
LEAD = 1.5
NOTE_CHARS = 120

# Built-in notes for the known places, used when the folder has no CLAUDE.md.
NOTES = {
    "work\\clients": "one folder per customer; client-specific work goes under the client",
    "work\\wf": "Rewst workflow exports, one folder per export",
    "work\\rewst": "Rewst projects not tied to one export",
    "work\\halo": "HaloPSA projects",
    "work\\ninja": "NinjaOne / NinjaRMM scripts, policies, alerts",
    "work\\cipp": "CIPP",
    "work\\m365": "Microsoft 365 / Entra / Intune",
    "work\\net": "Fortinet, VPN, certificates, DNS, firewalls",
    "work\\vendors": "vendor tooling (Huntress, Keeper, Keepit, Vircom, Blackpoint, Proofpoint, ScreenConnect)",
    "work\\scripts": "one-off scripts",
    "work": "paid work for the MSP",
    "home": "personal projects (new hobby projects go directly here)",
    "home\\homelab": "homelab: PXE, Supermicro, Traefik",
    "home\\ai": "local AI models, Qwen, Ollama, transcription",
    "cc": "tooling for Claude Code itself (launcher, session browser, repair, skills)",
}


@dataclass(frozen=True)
class Cand:
    rel: str  # relative to env.root, '\\' separated, on-disk spelling
    path: Path
    depth: int  # 1 = area
    stats: FolderStats = field(compare=False, hash=False)
    tokens: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Scored:
    cand: Cand
    score: float
    matched: tuple[str, ...] = ()  # the name's tokens this folder matched, in name order
    weights: tuple[float, ...] = ()  # their IDF, same order


@dataclass(frozen=True)
class Place:
    rel: str
    path: Path
    note: str


# ---- tokens ---------------------------------------------------------------------------------------------------------

def tokens(text: str | None) -> list[str]:
    """Distinct lower-case words of `text` (split on non-alphanumerics), 3+ characters, stop words removed."""
    out: list[str] = []
    for t in re.split(r"[^a-z0-9]+", (text or "").lower()):
        if len(t) >= 3 and t not in STOP and t not in out:
            out.append(t)
    return out


def stem_match(a: str, b: str) -> bool:
    """Equal, or both at least 5 characters and sharing their first 5."""
    return a == b or (len(a) >= 5 and len(b) >= 5 and a[:5] == b[:5])


# ---- candidates -----------------------------------------------------------------------------------------------------

def _rel(env: Env, path: Path) -> str:
    return str(Path(path).relative_to(env.root))


def candidates(env: Env, index, rows: list) -> list[Cand]:
    """Every folder to depth 4 below work and home and depth 2 below cc (areas included), with its stats and tokens
    (relative path words plus its 3 most recent session names). `index` is unused when `rows` is given; it is kept
    for the signature `tree.children` shares."""
    stats = folder_stats_map(rows)
    out: list[Cand] = []
    empty = FolderStats(0, None, [])

    def add(path: str, rel: str, key: tuple[str, ...], depth: int) -> None:
        st = stats.get(key, empty)
        words = tokens(rel.replace("\\", " ") + " " + " ".join(st.names or []))
        out.append(Cand(rel, Path(path), depth, FolderStats(st.sessions, st.last_activity, list(st.names)),
                        frozenset(words)))

    def walk(path: str, rel: str, key: tuple[str, ...], depth: int, limit: int) -> None:
        # os.scandir rather than tree.children: the same listing rule (`tree._listed`), but a few thousand
        # folders in well under a second, since the entries carry their directory flag
        for name, child in _subdirs(path):
            child_rel, child_key = rel + "\\" + name, key + (name.lower(),)
            add(child, child_rel, child_key, depth + 1)
            if depth < limit:
                walk(child, child_rel, child_key, depth + 1, limit)

    root_key = tuple(p.lower() for p in Path(env.root).parts)
    for name, path in _subdirs(str(env.root)):
        limit = DEPTH.get(name.lower())
        if limit is None:
            continue
        add(path, name, root_key + (name.lower(),), 1)
        walk(path, name, root_key + (name.lower(),), 1, limit)
    return out


def _subdirs(path: str) -> list[tuple[str, str]]:
    """(name, full path) of the sub-folders the picker lists (`tree._listed`'s rule: no `.`/`_` prefix, no
    node_modules), sorted case-insensitively."""
    try:
        with os.scandir(path) as it:
            found = [(e.name, e.path) for e in it if _is_dir(e)]
    except OSError:
        return []
    return sorted(((n, p) for n, p in found if not n.startswith((".", "_")) and n.lower() not in _SKIP_NAMES),
                  key=lambda x: x[0].lower())


def _is_dir(entry: os.DirEntry) -> bool:
    try:
        return entry.is_dir()
    except OSError:
        return False


# ---- rank -----------------------------------------------------------------------------------------------------------

def _parent(rel: str) -> str:
    return rel.lower().rpartition("\\")[0]


def idf_table(query: list[str], cands: list[Cand]) -> dict[str, float]:
    """IDF of each query token over the candidates' token sets, with stem-merged document frequency: a candidate
    counts when any of its tokens stem-matches the query token and its parent folder's tokens do not (the word
    starts there). Without that, a folder with a deep sub-tree would make its own name look common, since every
    folder below it carries the word in its path."""
    n = len(cands)
    by_rel = {c.rel.lower(): c for c in cands}
    out = {}
    for q in query:
        hits = {c.rel.lower() for c in cands if any(stem_match(q, t) for t in c.tokens)}
        df = sum(1 for rel in hits if _parent(rel) not in hits or _parent(rel) not in by_rel)
        out[q] = math.log((n + 1) / (1 + df))
    return out


HOME_PLACES = ("home\\homelab", "home\\ai")


def organisational(rel: str) -> bool:
    """Places to make folders in, never a PROJECT answer: the areas, `work\\<bucket>`, `work\\clients\\<client>`
    and the known home places (home\\homelab, home\\ai)."""
    parts = rel.lower().split("\\")
    return ((len(parts) == 1 and parts[0] in DEPTH) or (len(parts) == 2 and parts[0] == "work")
            or (len(parts) == 3 and parts[:2] == ["work", "clients"]) or rel.lower() in HOME_PLACES)


def rank(name: str, cands: list[Cand], now: float, k: int = 14) -> list[Scored]:
    """The `k` best non-organisational candidates for `name`, best first: the IDF sum of the name's tokens each one
    stem-matches, plus a small bonus for activity in the last 30 days. Candidates matching nothing are left out."""
    query = tokens(name)
    if not query:
        return []
    idf = idf_table(query, cands)
    scored = []
    for c in cands:
        if organisational(c.rel):
            continue
        hits = [q for q in query if any(stem_match(q, t) for t in c.tokens)]
        if not hits:
            continue
        weights = tuple(idf[q] for q in hits)
        score = sum(weights)
        if c.stats.last_activity and now - c.stats.last_activity < RECENT_S:
            score += RECENT_BONUS
        scored.append(Scored(c, score, tuple(hits), weights))
    scored = _one_per_branch_match(scored)
    scored.sort(key=lambda s: (-round(s.score, 9), -(s.cand.stats.last_activity or 0.0), -s.cand.depth,
                               s.cand.rel.lower()))
    return scored[:k]


def _leaf_hits(s: Scored) -> int:
    leaf = tokens(s.cand.rel.rpartition("\\")[2])
    return sum(1 for q in s.matched if any(stem_match(q, t) for t in leaf))


def _one_per_branch_match(scored: list[Scored]) -> list[Scored]:
    """Within one branch (a folder and its ancestors), a folder whose matched words are a subset of another's adds
    nothing: descendants carry their ancestors' path words and ancestors carry their descendants' recent session
    names. Keep the one that matches more words; on equal matches the one whose own folder name matches more of
    them (the project, not its container); then the shallower one (the project, not its `data\\out`)."""
    by_rel = {s.cand.rel.lower(): s for s in scored}
    drop: set[str] = set()
    for s in scored:
        rel = s.cand.rel.lower()
        mine = set(s.matched)
        anc = _parent(rel)
        while anc:
            a = by_rel.get(anc)
            if a is not None:
                theirs = set(a.matched)
                if mine > theirs:
                    drop.add(anc)
                elif theirs > mine:
                    drop.add(rel)
                elif theirs == mine:
                    drop.add(anc if _leaf_hits(s) > _leaf_hits(a) else rel)
            anc = _parent(anc)
    return [s for s in scored if s.cand.rel.lower() not in drop]


def _below(a: Cand, b: Cand) -> bool:
    """`a` is strictly inside `b`."""
    pa, pb = a.rel.lower().split("\\"), b.rel.lower().split("\\")
    return len(pa) > len(pb) and pa[:len(pb)] == pb


def same_branch(a: Cand, b: Cand) -> bool:
    return a.rel.lower() == b.rel.lower() or _below(a, b) or _below(b, a)


def strong(scored: list[Scored]) -> bool:
    """The top result is clear enough to move the picker without a model: it has sessions, at least STRONG_TOKENS
    rare tokens
    (IDF >= RARE_IDF) whose IDF sums to STRONG_SUM or more, and a score of at least LEAD times the runner-up's (the
    best result outside the top one's own branch: its ancestors and descendants are not rivals)."""
    if not scored:
        return False
    top = scored[0]
    if top.cand.stats.sessions < 1:
        return False  # a folder never worked in is a guess from its name, not a project the user is continuing
    rare = [w for w in top.weights if w >= RARE_IDF]
    if len(rare) < STRONG_TOKENS or sum(rare) < STRONG_SUM:
        return False
    rival = next((s for s in scored[1:] if not same_branch(s.cand, top.cand)), None)
    return rival is None or top.score >= LEAD * rival.score


# ---- places ---------------------------------------------------------------------------------------------------------

def _claude_md_note(folder: Path) -> str:
    """`<title>: <first plain line>` from `folder\\CLAUDE.md` (either part alone when the other is missing): the
    first heading's text and the first non-empty line that is not a heading, list item, table row, quote or code
    fence. Trimmed to NOTE_CHARS."""
    try:
        text = (folder / "CLAUDE.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    title = line = ""
    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            continue
        if s.startswith("#"):
            if not title:
                title = re.sub(r"^CLAUDE\.md\s*[—–:-]*\s*", "", s.lstrip("#").strip(), flags=re.I)
            continue
        if re.match(r"([-*+|>]|\d+[.)]|```)", s):
            continue
        line = s
        break
    note = f"{title}: {line}" if title and line else title or line
    return note[:NOTE_CHARS]


def _place(env: Env, path: Path) -> Place:
    """A place and its note: the built-in note for the known places (their CLAUDE.md files are instructions for
    agents, not descriptions), else the folder's CLAUDE.md, else none."""
    rel = _rel(env, path)
    return Place(rel, path, NOTES.get(rel.lower()) or _claude_md_note(path))


def places(env: Env, cands: list[Cand], name: str) -> list[Place]:
    """Where a new folder could go: every area (work, home, cc) and every `work\\<bucket>` on disk, the known home
    places (home\\homelab, home\\ai) when they exist, and `work\\clients\\<client>` for each client whose name
    stem-matches a word of `name`. Each carries the first plain line of its CLAUDE.md, else a built-in note."""
    out: list[Place] = []
    areas = [a for a in _listed(env.root) if a.name.lower() in DEPTH]
    for area in areas:
        out.append(_place(env, area))
        if area.name.lower() == "work":
            out.extend(_place(env, b) for b in _listed(area))
    for rel in HOME_PLACES:
        hit = next((c for c in cands if c.rel.lower() == rel), None)
        if hit is not None:
            out.append(_place(env, hit.path))
    query = tokens(name)
    for c in cands:
        parts = c.rel.lower().split("\\")
        if len(parts) == 3 and parts[:2] == ["work", "clients"] and \
                any(stem_match(q, t) for q in query for t in tokens(parts[2])):
            out.append(_place(env, c.path))
    return out
