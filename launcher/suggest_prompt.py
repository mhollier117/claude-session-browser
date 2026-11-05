"""The prompt both model stages (Qwen and Claude) receive: a fixed system message and a per-call user message that
lists the tree with session stats and the most recent sessions."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .display import age, rel_or_abs
from .index import Index, Row
from .paths import Env
from .tree import children

# Copied verbatim from the spec, "Prompt (stages 1 and 2, same content)" (the "Decide in this order" version).
SYSTEM_PROMPT = r"""You place a new coding-assistant session into an existing folder tree. Reply with JSON only.

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
_DASH = "—"


class _Snapshot:
    """One `index.rows()` result served to `tree.children` for every folder, so the index is read once."""

    def __init__(self, rows: list[Row]):
        self._rows = rows

    def rows(self, filter_text: str = "") -> list[Row]:
        return self._rows


@dataclass
class _Line:
    depth: int  # 0 = area
    text: str
    sessions: int
    last_activity: float | None


def _tree(env: Env, snap: _Snapshot) -> list[_Line]:
    now = env.now()
    out: list[_Line] = []

    def add(child, depth: int, limit: int) -> None:
        st = child.stats
        text = f"{rel_or_abs(env.root, child.path)} · {st.sessions} · {age(st.last_activity, now)}"
        if st.names:
            text += " · " + ", ".join(json.dumps(n, ensure_ascii=False) for n in st.names)
        out.append(_Line(depth, text, st.sessions, st.last_activity))
        if depth < limit:
            for grandchild in children(env, snap, child.path):
                add(grandchild, depth + 1, limit)

    for area in children(env, snap, env.root):
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


def build_user_message(env: Env, index: Index, session_name: str, max_lines: int = 400) -> str:
    """The user message: session name, the tree (depth 4 under work and home, 2 under cc, empty folders included as
    `path · 0 · —`), then the most recent sessions overall. Reads the index once."""
    rows = index.rows()
    snap = _Snapshot(rows)
    tree = _fit(_tree(env, snap), max_lines)
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
