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
import threading
import time
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
# tools/suggest_eval.py on the real tree (about 2,540 folders): strong for 4 of 29 names, 3 right; the fourth,
# work\ninja\ninja_rmm_alert_renewal_review for "Ninja RMM Alert Review", holds a session of nearly that name,
# though the eval expects the work\ninja place (a session of exactly that name started there). A top result whose
# score came from a folded empty sub-folder is never strong.
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
    folded: bool = False  # the score came from an empty sub-folder folded into this one (see _collapse_empty)


@dataclass(frozen=True)
class Place:
    rel: str
    path: Path
    note: str
    sessions_here: int = 0  # sessions whose cwd is exactly this folder
    names_here: tuple[str, ...] = ()  # the two newest of them


# ---- tokens ---------------------------------------------------------------------------------------------------------

def tokens(text: str | None) -> list[str]:
    """Distinct lower-case words of `text` (split on non-alphanumerics), stop words removed: 3+ characters, or
    2 letters written in capitals in `text` (acronyms such as CA, AD, VM, HR; a lower-case "ca" is dropped)."""
    out: list[str] = []
    for raw in re.split(r"[^A-Za-z0-9]+", text or ""):
        t = raw.lower()
        keep = len(t) >= 3 or (len(raw) == 2 and raw.isalpha() and raw.isupper())
        if keep and t not in STOP and t not in out:
            out.append(t)
    return out


def stem_match(a: str, b: str) -> bool:
    """Equal, or both at least 5 characters and sharing their first 5 (so a 2-letter acronym matches only
    itself)."""
    return a == b or (len(a) >= 5 and len(b) >= 5 and a[:5] == b[:5])


# ---- candidates -----------------------------------------------------------------------------------------------------

def _rel(env: Env, path: Path) -> str:
    return str(Path(path).relative_to(env.root))


WALK_TTL_S = 60.0
_walk_cache: dict[str, tuple[float, list[tuple[str, str, int]]]] = {}
_walk_lock = threading.Lock()


def _walk_tree(root: str) -> list[tuple[str, str, int]]:
    """(rel, full path, depth) of every folder to depth 4 below work and home and depth 2 below cc (areas
    included, depth 1). os.scandir rather than tree.children: the same listing rule (`tree._listed`), but a few
    thousand folders in well under a second, since the entries carry their directory flag."""
    out: list[tuple[str, str, int]] = []

    def walk(path: str, rel: str, depth: int, limit: int) -> None:
        for name, child in _subdirs(path):
            child_rel = rel + "\\" + name
            out.append((child_rel, child, depth + 1))
            if depth < limit:
                walk(child, child_rel, depth + 1, limit)

    for name, path in _subdirs(root):
        limit = DEPTH.get(name.lower())
        if limit is None:
            continue
        out.append((name, path, 1))
        walk(path, name, 1, limit)
    return out


def _structure(root: Path) -> list[tuple[str, str, int]]:
    """`_walk_tree(root)`, cached per process for WALK_TTL_S seconds (the folder layout only; stats and tokens are
    worked out from the current rows on every call). `invalidate_walk()` drops it."""
    key = str(root).lower()
    now = time.monotonic()
    with _walk_lock:
        hit = _walk_cache.get(key)
        if hit is not None and now - hit[0] < WALK_TTL_S:
            return hit[1]
    found = _walk_tree(str(root))
    with _walk_lock:
        _walk_cache[key] = (now, found)
    return found


def invalidate_walk() -> None:
    """Forget the cached folder walk (called when the launcher creates a folder)."""
    with _walk_lock:
        _walk_cache.clear()


def _org_prefix_len(parts: list[str]) -> int:
    """How many leading parts of a relative path are organisational ancestors (area, bucket, client, home place)."""
    n = 0
    for i in range(1, len(parts)):
        if organisational("\\".join(parts[:i])):
            n = i
    return n


def candidates(env: Env, index, rows: list) -> list[Cand]:
    """Every folder to depth 4 below work and home and depth 2 below cc (areas included), with its stats and tokens:
    the words of its own folder name (always), plus the words of its path between its organisational ancestors
    and itself and of its 3 most recent session names, minus the words of those ancestors' names. A client's or bucket's name is no evidence for one project in it (the
    client is offered as a PLACE instead), and as a path word shared by every folder below it, it would crowd the
    shortlist with that client's folders. `index` is unused; it is kept for the signature `tree.children` shares."""
    stats = folder_stats_map(rows)
    root_key = tuple(p.lower() for p in Path(env.root).parts)
    empty = FolderStats(0, None, [])
    out: list[Cand] = []
    for rel, path, depth in _structure(env.root):
        parts = rel.split("\\")
        st = stats.get(root_key + tuple(p.lower() for p in parts), empty)
        cut = _org_prefix_len(parts)
        org_words = set(tokens(" ".join(parts[:cut])))
        own = tokens(parts[-1])  # the folder's own name always counts, even a word its client or bucket shares
        rest = tokens(" ".join(parts[cut:-1]) + " " + " ".join(st.names or []))
        words = own + [t for t in rest if t not in org_words]
        out.append(Cand(rel, Path(path), depth, FolderStats(st.sessions, st.last_activity, list(st.names)),
                        frozenset(words)))
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
    scored = _one_per_branch_match(_collapse_empty(scored))
    scored.sort(key=_order)
    return _prefer_used(scored, k)


def _order(s: Scored):
    return (-round(s.score, 9), -(s.cand.stats.last_activity or 0.0), -s.cand.depth, s.cand.rel.lower())


def _collapse_empty(scored: list[Scored]) -> list[Scored]:
    """Fold each zero-session candidate into its nearest non-organisational ancestor that also scored: one entry
    per project subtree, on the ancestor's line, carrying the best score (A workflow family folder with sessions
    would otherwise be buried under its own dated, empty export folders.) The ancestor keeps its own matched
    words, so a container that only absorbed an empty project's words does not out-match a sibling project that
    has its own work; `folded` marks a score taken from below, which `strong` never trusts."""
    by_rel = {s.cand.rel.lower(): s for s in scored}
    for s in sorted(scored, key=lambda s: -s.cand.depth):  # deepest first, so a chain folds all the way up
        rel = s.cand.rel.lower()
        if s.cand.stats.sessions or rel not in by_rel:
            continue
        anc = _parent(rel)
        while anc and (organisational(anc) or anc not in by_rel):
            anc = "" if organisational(anc) else _parent(anc)
        if not anc:
            continue
        a, s = by_rel[anc], by_rel.pop(rel)
        by_rel[anc] = Scored(a.cand, max(a.score, s.score), a.matched, a.weights,
                             folded=a.folded or s.score > a.score)
    return list(by_rel.values())


def _prefer_used(scored: list[Scored], k: int) -> list[Scored]:
    """The best `k`, where a zero-session folder is kept only when it scores at least as well as the best folder
    with sessions, or to fill the slots that folders with sessions leave empty. `scored` is sorted best first."""
    used = [s for s in scored if s.cand.stats.sessions]
    if not used:
        return scored[:k]
    best = used[0].score
    keep = [s for s in scored if s.cand.stats.sessions or s.score >= best][:k]
    if len(keep) < k:
        chosen = {id(s) for s in keep}
        keep += [s for s in scored if id(s) not in chosen][:k - len(keep)]
        keep.sort(key=_order)
    return keep


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
    if top.folded:
        return False  # its best words are an empty sub-folder's, not evidence of this project's own work
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
        text = (folder / "CLAUDE.md").read_text(encoding="utf-8-sig")  # a BOM must not hide the heading
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


def _place(env: Env, path: Path, here: dict) -> Place:
    """A place and its note: the built-in note for the known places (their CLAUDE.md files are instructions for
    agents, not descriptions), else the folder's CLAUDE.md, else none; plus the sessions started in it directly."""
    rel = _rel(env, path)
    names = here.get(tuple(p.lower() for p in Path(path).parts), [])
    return Place(rel, path, NOTES.get(rel.lower()) or _claude_md_note(path), len(names), tuple(names[:2]))


def _sessions_here(rows: list) -> dict[tuple[str, ...], list[str]]:
    """Session names (newest first) by the lower-cased parts of their exact cwd."""
    out: dict[tuple[str, ...], list[str]] = {}
    for r in sorted((r for r in rows if r.cwd), key=lambda r: -r.last_activity):
        out.setdefault(tuple(p.lower() for p in Path(r.cwd).parts), []).append(r.display_name)
    return out


def places(env: Env, cands: list[Cand], name: str, rows: list = ()) -> list[Place]:
    """Where a new folder could go: every area (work, home, cc) and every `work\\<bucket>` on disk, the known home
    places (home\\homelab, home\\ai) when they exist, and `work\\clients\\<client>` for each client whose name
    stem-matches a word of `name`. Each carries a note (see `_place`) and the count and two newest names of the
    sessions in `rows` whose cwd is exactly that folder (a session can start in a place itself)."""
    here = _sessions_here(list(rows))
    out: list[Place] = []
    areas = [a for a in _listed(env.root) if a.name.lower() in DEPTH]
    for area in areas:
        out.append(_place(env, area, here))
        if area.name.lower() == "work":
            out.extend(_place(env, b, here) for b in _listed(area))
    for rel in HOME_PLACES:
        hit = next((c for c in cands if c.rel.lower() == rel), None)
        if hit is not None:
            out.append(_place(env, hit.path, here))
    query = tokens(name)
    for c in cands:
        parts = c.rel.lower().split("\\")
        if len(parts) == 3 and parts[:2] == ["work", "clients"] and \
                any(stem_match(q, t) for q in query for t in tokens(parts[2])):
            out.append(_place(env, c.path, here))
    return out
