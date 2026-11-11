"""The prompt both model stages (Qwen and Claude) receive.

v2 (in use): a fixed system message and a short user message that numbers the retrieved PROJECT candidates and the
PLACE candidates (see `retrieve`); the model answers with a number. v1 (kept for reference, no longer sent): the
whole tree with session stats and the most recent sessions, answered with a free-text path."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .display import age, rel_or_abs
from .index import Index
from .paths import Env
from .retrieve import Place, Scored, stem_match, tokens
from .tree import children, folder_stats_map

SYSTEM_PROMPT = r"""You file a new coding-assistant session into an existing folder tree. Reply with JSON only.

You get the session name and a numbered list of candidates of two kinds. A PROJECT is an existing project folder, shown with its session count and recent session names. A PLACE is an area, a bucket or a client folder where a new project folder can be made, shown with a note on what belongs there.

Pick a PROJECT when the session is more work on that same project or the same system, even if the task itself is new. A bug in the Halo ticket list sync belongs in the existing ticket list sync project. Judge by the subject: a PROJECT that shares only one generic word with the session name (alert, review, app, report, workflow, user, sync) is not a match.
Otherwise pick the PLACE that fits the subject and name a new folder for it: lower case, words joined by _, 2 to 4 words, no dates. Client work goes under that client's folder when it is listed. Personal or hobby work goes under home (or a home place), never under a work bucket. The reverse holds too: work for the MSP or its clients (FortiGate, VPN, certificates, Halo, Ninja, CIPP, Rewst, Microsoft 365) goes under work, not into a home or homelab project, unless the name says homelab.
Always answer with one of the numbers. Never return {}. If the name means nothing to you, pick the most general fitting PLACE, invent a short folder name and say confidence low.

Output JSON: {"choice": <number>, "new_folder": "<name or null>", "confidence": "high|medium|low", "reason": "<one sentence>"}
new_folder is null for a PROJECT and a name for a PLACE.

Example 1. Session name: Halo ticket list sync bug
1. PROJECT  work\halo\view_management  · 12 sessions · recent: "ticket list view sync", "halo view columns"
2. PROJECT  work\halo\contact_sync  · 4 sessions · recent: "contact sync"
3. PLACE    work\halo  — HaloPSA projects
Answer: {"choice": 1, "new_folder": null, "confidence": "high", "reason": "More work on the existing Halo ticket list sync project."}

Example 2. Session name: Ninja RMM Alert Review
1. PROJECT  work\wf\mediko_alert_flow  · 3 sessions · recent: "Mediko alert review"
2. PLACE    work\ninja  — NinjaOne / NinjaRMM scripts, policies, alerts
Answer: {"choice": 2, "new_folder": "rmm_alert_review", "confidence": "medium", "reason": "Only the generic words alert and review match the Mediko project; NinjaRMM work goes in the ninja bucket."}

Example 3. Session name: razer synapse app reverse engineer
1. PROJECT  work\vendors\keeper\app_review  · 2 sessions · recent: "keeper app review"
2. PLACE    work\vendors  — vendor tooling
3. PLACE    home  — personal projects (new hobby projects go directly here)
Answer: {"choice": 3, "new_folder": "razer_synapse_re", "confidence": "medium", "reason": "A personal hobby project, so a new folder directly under home."}

Example 4. Session name: homelab pxe boot order
1. PROJECT  home\homelab\pxe  · 6 sessions · recent: "pxe menu", "ipxe chain"
2. PLACE    home\homelab  — homelab: PXE, Supermicro, Traefik
Answer: {"choice": 1, "new_folder": null, "confidence": "high", "reason": "The existing PXE project in the homelab."}"""


@dataclass(frozen=True)
class Choice:
    kind: Literal["PROJECT", "PLACE"]
    rel: str
    path: Path


def _evidence(names: list[str], query: list[str], n: int = 2) -> list[str]:
    """The `n` of a folder's recent session names (newest first) that share the most words with the query; ties
    keep the newest. So the line shows why the folder was retrieved."""
    def hits(text: str) -> int:
        words = tokens(text)
        return sum(1 for q in query if any(stem_match(q, w) for w in words))
    return [nm for _i, nm in sorted(enumerate(names), key=lambda p: (-hits(p[1]), p[0]))][:n]


def build_user_message(name: str, scored: list[Scored], places: list[Place]) -> tuple[str, list[Choice]]:
    """The v2 user message and the choice each number stands for: the ranked PROJECT candidates (a folder that is
    also a PLACE is listed once, as the place), each with the two of its 3 most recent session names that best
    match the name, then the PLACE candidates."""
    place_rels = {p.rel.lower() for p in places}
    query = tokens(name)
    lines: list[str] = []
    choices: list[Choice] = []
    for s in scored:
        c = s.cand
        if c.rel.lower() in place_rels:
            continue
        choices.append(Choice("PROJECT", c.rel, c.path))
        text = f"{len(choices)}. PROJECT  {c.rel}  · {c.stats.sessions} sessions"
        recent = ", ".join(json.dumps(n, ensure_ascii=False) for n in _evidence(c.stats.names or [], query))
        lines.append(text + (f" · recent: {recent}" if recent else ""))
    for p in places:
        choices.append(Choice("PLACE", p.rel, p.path))
        lines.append(f"{len(choices)}. PLACE    {p.rel}" + (f"  — {p.note}" if p.note else ""))
    clean = " ".join(name.split())
    return f"Session name: {clean}\n\nCandidates:\n" + "\n".join(lines) + "\n", choices


# Copied verbatim from the spec, "Prompt (stages 1 and 2, same content)" (the "Decide in this order" version).
SYSTEM_PROMPT_V1 = r"""You place a new coding-assistant session into an existing folder tree. Reply with JSON only.

Tree root: C:\claude-code. Levels:
- L1 areas: work (paid work for the MSP), home (personal: homelab, local AI, hobby projects), cc (tooling for Claude Code itself: session repair, session browser, launcher, skills, reorg).
- L2 buckets under work: clients (per-client folders, one per customer), wf (Rewst workflow exports, folder per export name and date), rewst (Rewst projects not tied to one export), halo (HaloPSA projects), ninja (NinjaOne scripts), cipp (CIPP), m365 (Microsoft 365 / Entra / Intune), net (Fortinet, VPN, certificates, DNS, firewalls), vendors (vendor tooling: Huntress, Keeper, Keepit, Vircom, Blackpoint, Proofpoint, ScreenConnect), scripts (one-off scripts).
- Under home: homelab (PXE, Supermicro, fgt2, Traefik, the lab network), ai (local models, Qwen, Ollama), or a project folder directly under home.
- Under clients: one folder per client, then project folders, nested as deeply as the work needs.
- L1, L2 and clients\<client> are organisational: a session is rarely started directly in them.

Decide in this order:
1. MATCH: look for an existing folder in the tree whose own name, or whose recent session names, share a distinctive word with the session name (ignore words like new, fix, phase, update). If one exists, return exactly that folder as path and set new_folder to null. Prefer the deepest match with the most recent activity. Do not propose a new folder when a match exists.
2. PLACE: only if nothing matches, choose the best parent (the client folder, named exactly as it exists, if the work is for a customer; else the bucket) and propose ONE new folder name: lower case, words joined by _, no dates unless the parent uses them (wf does).
3. Never propose a folder directly under the root.
confidence: high = step 1 matched; medium = step 2 with a clear parent; low = otherwise.

Output JSON: {"path": "<relative to C:\claude-code, existing folder>", "new_folder": "<name or null>", "confidence": "high|medium|low", "reason": "<one sentence naming the matched folder or the chosen parent>"}"""

_AREA_DEPTH = {"work": 4, "home": 4, "cc": 2}  # levels listed below each area
_RECENT = 15


@dataclass
class _Line:
    depth: int  # 0 = area
    text: str
    sessions: int
    last_activity: float | None


def _tree(env: Env, index: Index, stats: dict) -> list[_Line]:
    now = env.now()
    out: list[_Line] = []

    def add(child, depth: int, limit: int) -> None:
        st = child.stats
        text = f"{rel_or_abs(env.root, child.path)} · {st.sessions} · {age(st.last_activity, now)}"
        if st.names:
            text += " · " + ", ".join(json.dumps(n, ensure_ascii=False) for n in st.names)
        out.append(_Line(depth, text, st.sessions, st.last_activity))
        if depth < limit:
            for grandchild in children(env, index, child.path, stats):
                add(grandchild, depth + 1, limit)

    for area in children(env, index, env.root, stats):
        limit = _AREA_DEPTH.get(area.name.lower())
        if limit is not None:
            add(area, 0, limit)
    return out


def _fit(lines: list[_Line], max_lines: int) -> list[_Line]:
    """Drop lines until `max_lines` remain: empty folders first (deepest first), then the folders with the oldest
    activity. Areas and the level right under them (the buckets) always stay."""
    if len(lines) <= max_lines:
        return lines
    droppable = sorted((i for i, ln in enumerate(lines) if ln.depth > 1),
                       key=lambda i: (lines[i].sessions > 0,
                                      -lines[i].depth if lines[i].sessions == 0 else lines[i].last_activity or 0.0))
    drop = set(droppable[:len(lines) - max_lines])
    return [ln for i, ln in enumerate(lines) if i not in drop]


def build_user_message_v1(env: Env, index: Index, session_name: str, max_lines: int = 400) -> str:
    """The v1 user message (reference only; not sent any more): session name, the tree (depth 4 under work and home, 2 under cc, empty folders included as
    `path · 0 · —`), then the most recent sessions overall. Reads `index.rows()` once and groups the rows into
    folder stats in one pass; `index` can be any object with that method."""
    rows = index.rows()
    tree = _fit(_tree(env, index, folder_stats_map(rows)), max_lines)
    recent = [r for r in rows if r.cwd][:_RECENT]
    name = " ".join(session_name.split())
    parts = [
        f"Session name: {name}",
        "",
        "Folder tree (depth 4 under work and home, depth 2 under cc; each line: path · sessions · last activity · "
        "up to 3 recent session names):",
        *(ln.text for ln in tree),
        "",
        f"Most recent {_RECENT} sessions overall (name → folder):",
        *(f"{json.dumps(r.display_name, ensure_ascii=False)} → {rel_or_abs(env.root, Path(r.cwd))}" for r in recent),
    ]
    return "\n".join(parts) + "\n"
