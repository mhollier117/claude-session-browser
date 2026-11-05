"""The project tree the launcher browses: what each level is, its children with stats, and fragment jumps."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .index import FolderStats, Index
from .paths import Env

Kind = Literal["area", "bucket", "client", "project", "container", "root"]

_AREAS = {"work", "home", "cc"}
_SKIP_NAMES = {"node_modules"}


@dataclass
class Child:
    name: str
    path: Path
    kind: Kind
    stats: FolderStats


def _rel_parts(root: Path, folder: Path) -> tuple[str, ...]:
    try:
        return tuple(p.lower() for p in Path(folder).relative_to(root).parts)
    except ValueError:
        return tuple(p.lower() for p in Path(folder).parts)


def level_kind(root: Path, folder: Path) -> Kind:
    parts = _rel_parts(root, folder)
    if not parts:
        return "root"
    if len(parts) == 1 and parts[0] in _AREAS:
        return "area"
    if parts[0] == "work":
        if len(parts) == 2:
            return "bucket"
        if len(parts) == 3 and parts[1] == "clients":
            return "client"
    return "project"


def new_folder_kind_options(root: Path, parent: Path) -> list[str]:
    """Kinds a new folder under `parent` can take, default first."""
    parts = _rel_parts(root, parent)
    if parts == ("work", "clients"):
        return ["client"]
    if parts and parts[0] == "work" and len(parts) <= 2:
        return ["organisational"]
    return ["project", "container"]


def _listed(folder: Path) -> list[Path]:
    try:
        entries = [p for p in folder.iterdir() if p.is_dir()]
    except OSError:
        return []
    return sorted((p for p in entries if not p.name.startswith((".", "_")) and p.name.lower() not in _SKIP_NAMES),
                  key=lambda p: p.name.lower())


def children(env: Env, index: Index, folder: Path) -> list[Child]:
    """Sub-folders of `folder` with their session stats. The index is read once for all children; a session counts
    for a child when its cwd is that folder or beneath it (case-insensitive, whole path components)."""
    rows = index.rows()
    out = []
    for path in _listed(folder):
        want = tuple(p.lower() for p in path.parts)
        hits = sorted((r for r in rows if r.cwd and tuple(p.lower() for p in Path(r.cwd).parts)[:len(want)] == want),
                      key=lambda r: -r.last_activity)
        stats = FolderStats(sessions=len(hits), last_activity=max((r.last_activity for r in hits), default=None),
                            names=[r.display_name for r in hits[:3]])
        out.append(Child(path.name, path, level_kind(env.root, path), stats))
    return out


def resolve_fragment(env: Env, folder: Path, fragment: str) -> list[Path]:
    """Walk `a/b/c` from `folder`, each piece prefix-matching sub-folder names (case-insensitive) one level down;
    returns every full match."""
    paths = [folder]
    for piece in (p for p in fragment.split("/") if p):
        needle = piece.lower()
        paths = [c for base in paths for c in _listed(base) if c.name.lower().startswith(needle)]
        if not paths:
            return []
    return paths


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "_", name.lower()).strip("_")
