"""The new-session walk: a pure state machine (`NewFlow`) and its renderer (`render_new`).

Steps, in order: name → picker (the suggestion is applied live) → folder name → kind → wrapper → GitHub account
(project only) → done. The picker walks the tree one level at a time; each level lists its child folders with
session stats, `● start here` (not at the root) and `+ new folder here…`. Nothing here touches the terminal; all
Index use stays on the calling (main) thread.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from ..display import age, rel_or_abs
from ..index import FolderStats, Index
from ..launch import WRAPPERS, Launch, default_wrapper, new_command
from ..paths import Env
from ..scaffold import Runner, check_name, create_folder
from ..suggest import Pending, Suggestion, default_http, suggest
from ..tree import children, folder_stats_map, new_folder_kind_options, resolve_fragment, slug
from .resume import fit

_KIND_LABELS = {"project": ("a project", "start the session in it · git init · .claude-project · plan.md"),
                "container": ("a container", "a grouping folder only · then keep choosing inside it"),
                "client": ("a client", "writes a CLAUDE.md stub naming the client"),
                "organisational": ("organisational", "a grouping folder")}
_GITHUB = (("personal", "personal", "mhollier117"), ("work", "work", "mhollierBPT"))
_SEP = " › "
_NAME_COL = 28
PICKER_KEYS = "type to filter (a/b jumps, /a/b from the root) · ↑↓ · enter opens · backspace up · esc root · ctrl-c quit"
OPTION_KEYS = "↑↓ · enter choose · esc back · ctrl-c quit"
TEXT_KEYS = "enter confirm · esc back · ctrl-c quit"


def _tools(rows) -> dict[tuple[str, ...], str]:
    """The tool (store) of the newest session at or below each folder, keyed like `folder_stats_map`."""
    out: dict[tuple[str, ...], str] = {}
    for r in sorted((r for r in rows if r.cwd), key=lambda r: -r.last_activity):
        parts = tuple(p.lower() for p in Path(r.cwd).parts)
        for i in range(1, len(parts) + 1):
            out.setdefault(parts[:i], r.store)
    return out


def _stats_text(st: FolderStats, tool: str | None, now: float) -> str:
    if not st.sessions:
        return "no sessions"
    when = age(st.last_activity, now)
    when = f"{when} ago" if when[:1].isdigit() else when
    text = f"{st.sessions} session{'' if st.sessions == 1 else 's'} · {when}"
    return f"{text} · {tool}" if tool else text


class NewFlow:
    """What the new-session screens show and what each key does. `start(name)` begins the walk; the app routes
    keys to `type_filter`/`backspace`/`move`/`enter`/`esc` and polls `poll()` for the model's answer."""

    def __init__(self, env: Env, index: Index, runner: Runner | None = None, http=default_http, *,
                 suggest_fn=suggest):
        self.env = env
        self.index = index
        self.runner = runner
        self.http = http
        self._suggest = suggest_fn
        snapshot = index.rows()  # once: stats and tools for every level come from this list
        self._stats = folder_stats_map(snapshot)
        self._tool = _tools(snapshot)
        self.step = "name"
        self.name = ""
        self.text = ""  # the text being edited on the name and folder-name steps
        self.error = ""
        self.status = ""  # set by the app while a folder is being created (git/gh can take a while)
        self.folder = env.root
        self.filter = ""
        self.rows: list[tuple[str, str, Path | None]] = []
        self.selected = 0
        self.moved = False  # the user has acted in the picker: a late suggestion is offered, not applied
        self.pending: Pending | None = None
        self.suggestion: Suggestion | None = None  # the one the picker jumped to (shown as the banner)
        self.offered: Suggestion | None = None  # a late one, shown as a ★ row at the top
        self._model_applied = False
        self._preselect: str | None = None  # `+ new folder: <name>` from a suggestion, for self.folder only
        self.new_name: str | None = None  # None: start here (no new folder)
        self.new_kind: str | None = None
        self.wrapper: str | None = None
        self.github: str | None = None
        self.cwd: Path | None = None
        self.session_id: str | None = None
        self.warnings: list[str] = []

    # ---- name -------------------------------------------------------------------------------------------------------

    def start(self, name: str) -> NewFlow:
        """Begin the walk for session `name` (required); the picker opens at the local suggestion at once and the
        model stages run in the background (see `poll`)."""
        name = (name or "").strip()
        if not name:
            raise ValueError("a session name is required")
        self.name = name
        self.step = "picker"
        self.error = ""
        self.pending = self._suggest(self.env, self.index, name, http=self.http, runner=self.runner,
                                     on_update=lambda s: None)
        self._jump(self.pending.local)
        return self

    def poll(self) -> bool:
        """Apply the model's answer once it is there (called by the app's timer); True when it changed the view."""
        if self.pending is None or self._model_applied:
            return False
        s = self.pending.wait(0)
        if s is None:
            return False
        self._model_applied = True
        if self.step != "picker":
            return False
        self.apply_suggestion(s)
        return True

    def asking(self) -> bool:
        """The model stages are still running and have not answered."""
        return (self.pending is not None and not self._model_applied and self.pending.wait(0) is None
                and not self.pending.join(0))

    # ---- picker -----------------------------------------------------------------------------------------------------

    def apply_suggestion(self, s: Suggestion) -> None:
        """Jump to `s` if the user has not acted in the picker yet; otherwise offer it as a ★ row at the top.
        Ignored once the walk has left the picker."""
        if self.step != "picker":
            return
        if not self.moved:
            self._jump(s)
        else:
            self.offered = s
            self._refresh(select_first=False)

    def _jump(self, s: Suggestion) -> None:
        self.suggestion = s
        self.offered = None
        self._open(s.path, preselect=s.new_folder)

    def _open(self, folder: Path, preselect: str | None = None) -> None:
        self.folder = Path(folder)
        self.filter = ""
        self._preselect = preselect
        self._refresh()

    def _refresh(self, select_first: bool = True) -> None:
        """Rebuild the rows. `select_first`: select the preselected new-folder row, else the first row that is not
        the ★ offer (enter must go where the user typed, not to the offer); otherwise keep the selected row."""
        current = self.rows[self.selected] if self.rows and not select_first else None
        self.rows = self._build_rows()
        kinds = [k for k, _l, _p in self.rows]
        if current in self.rows:
            self.selected = self.rows.index(current)
        elif self._preselect and "new_named" in kinds:
            self.selected = kinds.index("new_named")
        else:
            self.selected = next((i for i, k in enumerate(kinds) if k != "suggested"), 0)

    def _build_rows(self) -> list[tuple[str, str, Path | None]]:
        rows: list[tuple[str, str, Path | None]] = []
        if self.offered is not None and Path(self.offered.path) != self.folder:
            s = self.offered
            rows.append(("suggested", f"★ {rel_or_abs(self.env.root, s.path)} — suggested by {s.source}"
                                      + (f" · {s.reason}" if s.reason else ""), Path(s.path)))
        if "/" in self.filter:  # a fragment: `a/b` from this level, `/a/b` from the root
            base = self.env.root if self.filter.startswith("/") else self.folder
            for p in resolve_fragment(self.env, base, self.filter):
                rows.append(("match", self.crumb(p), p))
            return rows
        kids = [c for c in children(self.env, self.index, self.folder, self._stats)
                if self.filter.lower() in c.name.lower()]
        width = min(_NAME_COL, max((len(c.name) for c in kids), default=0))
        now = self.env.now()
        for c in kids:
            stats = _stats_text(c.stats, self._tool.get(tuple(p.lower() for p in c.path.parts)), now)
            rows.append(("child", f"{fit(c.name, width)}  {stats}", c.path))
        if self.folder != self.env.root:
            rows.append(("start", "● start here", None))
        if self._preselect:
            rows.append(("new_named", f"+ new folder: {self._preselect}", None))
        rows.append(("new", "+ new folder here…", None))
        return rows

    def crumb(self, folder: Path) -> str:
        rel = rel_or_abs(self.env.root, folder)
        return str(folder) if rel in (".", str(folder)) else _SEP.join(Path(rel).parts)

    def breadcrumb(self) -> str:
        return self.crumb(self.folder)

    def banner(self) -> str:
        """`suggested by <source> · <reason>` while the picker shows the folder it jumped to."""
        s = self.suggestion
        if s is None or s.source == "root" or Path(s.path) != self.folder:
            return ""
        return f"suggested by {s.source}" + (f" · {s.reason}" if s.reason else "")

    def _act(self) -> None:
        self.moved = True
        self.error = ""

    def move(self, delta: int) -> None:
        if self.step == "picker":
            self._act()
        n = len(self.rows) if self.step == "picker" else len(self.options())
        if n:
            self.selected = max(0, min(n - 1, self.selected + delta))

    def type_filter(self, text: str) -> None:
        self._act()
        self.filter = text
        self._refresh()

    def backspace(self) -> None:
        """Delete from the filter; on an empty filter go up one level (not above the root)."""
        if self.filter:
            self.type_filter(self.filter[:-1])
        else:
            self.back()

    def back(self) -> None:
        self._act()
        if self.folder != self.env.root and self.env.root in self.folder.parents:
            self._open(self.folder.parent)
        else:
            self._open(self.env.root)

    def esc(self) -> None:
        """Picker: back to the root. Later steps: back to the step before."""
        if self.step in ("picker", "name"):
            if self.step == "picker":
                self._act()
                self._open(self.env.root)
            return
        self.error = ""
        if self.step == "folder_name":
            self.step = "picker"
            self._refresh()
        elif self.step == "kind":
            self._to_folder_name(self.new_name or "")
        elif self.step == "wrapper":
            if self.new_name is None:  # start here
                self.step = "picker"
                self._refresh()
            else:  # a project: back to its kind
                self._to_kind()
        elif self.step == "github":
            self._to_wrapper()

    # ---- enter ------------------------------------------------------------------------------------------------------

    def enter(self) -> None:
        if self.step == "name":
            try:
                self.start(self.text)
            except ValueError as e:
                self.error = str(e)
            return
        if self.step == "picker":
            self._enter_picker()
        elif self.step == "folder_name":
            self.submit_folder_name(self.text)
        elif self.step in ("kind", "wrapper", "github"):
            opts = self.options()
            if opts:
                value = opts[self.selected][0]
                {"kind": self.choose_kind, "wrapper": self.choose_wrapper, "github": self.choose_github}[self.step](
                    value)

    def _enter_picker(self) -> None:
        if not self.rows:
            return
        kind, _label, path = self.rows[self.selected]
        self._act()
        if kind in ("child", "match", "suggested"):
            preselect = self.offered.new_folder if kind == "suggested" and self.offered else None
            if kind == "suggested":
                self.suggestion, self.offered = self.offered, None
            self._open(path, preselect=preselect)
        elif kind == "start":
            self.new_name = self.new_kind = None
            self._to_wrapper()
        elif kind == "new":
            self._to_folder_name(slug(self.name))
        elif kind == "new_named":
            self._to_folder_name(self._preselect or slug(self.name))

    # ---- new folder ---------------------------------------------------------------------------------------------------

    def _to_folder_name(self, prefill: str) -> None:
        self.step = "folder_name"
        self.text = prefill

    def _kind_values(self) -> list[str]:
        return new_folder_kind_options(self.env.root, self.folder)

    def _name_problem(self, name: str) -> str:
        try:
            check_name(name)
        except ValueError as e:
            return str(e)
        return f"{name} already exists here" if (self.folder / name).exists() else ""

    def submit_folder_name(self, name: str) -> None:
        """Check the name (same rule as `create_folder`) and that it is free, then ask the kind; where the level only
        allows an organisational folder there is no question and it is created at once."""
        name = name.strip()
        self.error = self._name_problem(name)
        if self.error:
            return
        self.new_name = name
        if self._kind_values() == ["organisational"]:
            self.new_kind = "organisational"
            self._walk_into_new()
        else:
            self._to_kind()

    def _to_kind(self) -> None:
        self.step = "kind"
        values = self._kind_values()
        self.selected = values.index(self.new_kind) if self.new_kind in values else 0

    def choose_kind(self, kind: str) -> None:
        if kind not in self._kind_values():
            raise ValueError(f"{kind!r} is not offered here")
        self.new_kind = kind
        if kind == "project":
            self._to_wrapper()
        else:  # a container or client is a grouping folder: create it and keep walking inside it
            self._walk_into_new()

    def choose_new_folder(self, name: str, kind: str | None = None) -> None:
        """Name the new folder and, when given, its kind (the name and kind steps in one call)."""
        self.submit_folder_name(name)
        if kind is not None and self.step == "kind":
            self.choose_kind(kind)

    # ---- wrapper, github, create ----------------------------------------------------------------------------------------

    def target(self) -> Path:
        return self.folder / self.new_name if self.new_name else self.folder

    def _to_wrapper(self) -> None:
        self.step = "wrapper"
        values = [v for v, _l, _h in self._wrapper_options()]
        want = self.wrapper or default_wrapper(self.env.root, self.target())
        self.selected = values.index(want) if want in values else 0

    def _wrapper_options(self) -> list[tuple[str, str, str]]:
        # the Claude wrappers, then qwen as the other tool
        return [(w, w, "") for w in WRAPPERS if w != "qwen"] + [("qwen", "qwen", "the Qwen Code tool")]

    def choose_wrapper(self, wrapper: str) -> None:
        if wrapper not in WRAPPERS:
            raise ValueError(f"unknown wrapper: {wrapper!r}")
        self.wrapper = wrapper
        if self.new_name and self.new_kind == "project":
            self.step = "github"
            self.selected = [v for v, _l, _h in _GITHUB].index(self.github or "personal")
        else:
            self._create()

    def choose_github(self, account: str) -> None:
        if account not in ("personal", "work"):
            raise ValueError(f"unknown GitHub account: {account!r}")
        self.github = account
        self._create()

    def creating(self) -> Path | None:
        """The folder `enter` is about to create on this step (so the app can say so before git/gh block it)."""
        if self.step == "github" or (self.step == "kind" and self.options()
                                     and self.options()[self.selected][0] != "project"):
            return self.target()
        if self.step == "folder_name" and self._kind_values() == ["organisational"] \
                and not self._name_problem(self.text.strip()):
            return self.folder / self.text.strip()
        return None

    def _create_new(self) -> Path | None:
        """`create_folder` for the pending new folder; on failure back to the folder-name step with the error."""
        try:
            created = create_folder(self.folder, self.new_name, self.new_kind, self.name, runner=self.runner,
                                    github=self.github if self.new_kind == "project" else None)
        except (ValueError, OSError) as e:
            self.error = str(e) if isinstance(e, ValueError) else f"could not create the folder: {e}"
            self._to_folder_name(self.new_name)
            return None
        self.warnings.extend(created.warnings)
        return created.path

    def _walk_into_new(self) -> None:
        path = self._create_new()
        if path is None:
            return
        self.new_name = self.new_kind = None
        self.step = "picker"
        self._open(path)

    def _create(self) -> None:
        """Start here, or create the new project: then the walk is done."""
        if self.new_name:
            path = self._create_new()
            if path is None:
                return
            self.cwd = path
        else:
            self.cwd = self.folder
        self.session_id = str(uuid.uuid4())
        self.step = "done"

    def result(self) -> Launch | None:
        if self.step != "done":
            return None
        launch = new_command(self.cwd, self.name, self.wrapper, session_id=self.session_id)
        launch.warnings.extend(self.warnings)
        return launch

    # ---- option steps -----------------------------------------------------------------------------------------------

    def options(self) -> list[tuple[str, str, str]]:
        """(value, label, hint) for the kind, wrapper and GitHub steps; [] elsewhere."""
        if self.step == "kind":
            return [(k, *_KIND_LABELS[k]) for k in self._kind_values()]
        if self.step == "wrapper":
            return self._wrapper_options()
        if self.step == "github":
            return list(_GITHUB)
        return []

    def summary(self) -> list[str]:
        """The `→ mkdir …` / `→ cd … ; <wrapper> -n "…"` lines for what will run."""
        lines = []
        if self.new_name:
            extra = " ; git init ; plan.md" if self.new_kind == "project" else ""
            if self.new_kind == "project" and self.github:
                extra += f" ; GitHub ({self.github})"
            lines.append(f"→ mkdir {self.target()}{extra}")
        if self.step in ("wrapper", "github", "done"):
            wrapper = self.options()[self.selected][0] if self.step == "wrapper" else self.wrapper
            argv = new_command(self.target(), self.name, wrapper).argv  # the same flags the launch will use
            run = " ".join(f'"{a}"' if a == self.name and i == 2 else a for i, a in enumerate(argv))
            lines.append(f"→ cd {self.target()} ; {run}")
        return lines


# ---- rendering ------------------------------------------------------------------------------------------------------

def render_new(flow: NewFlow, width: int, height: int) -> list[tuple[str, str]]:
    """The current step as prompt_toolkit (style, text) fragments."""
    frags: list[tuple[str, str]] = []

    def line(*parts: tuple[str, str]) -> None:
        frags.extend(parts)
        frags.append(("", "\n"))

    if flow.status:
        line(("class:status", fit(flow.status, width)))

    if flow.step == "name":
        line(("class:title", "Session name: "), ("", flow.text), ("class:dim", "▏"))
        if flow.error:
            line(("class:status", flow.error))
        line()
        line(("class:footer", TEXT_KEYS))
        return frags

    line(("class:title", "Session name: "), ("", flow.name))
    line()
    if flow.step == "picker":
        _render_picker(flow, width, height, line)
        return frags
    new_folder = flow.new_name is not None or flow.step in ("folder_name", "kind")
    line(("class:title", flow.breadcrumb()), ("class:dim", "  ·  new folder here" if new_folder else "  ·  start here"))
    line()
    if flow.step == "folder_name":
        line(("class:title", "Folder name: "), ("", flow.text), ("class:dim", "▏"))
        if flow.error:
            line(("class:status", flow.error))
        line()
        line(("class:footer", TEXT_KEYS))
        return frags
    if flow.new_name and flow.step != "kind":
        line(("class:title", "Folder name: "), ("", flow.new_name), ("class:dim", f"  ({flow.new_kind})"))
    title = {"kind": "What is it?", "wrapper": "Launch with", "github": "GitHub account"}.get(flow.step, "")
    line(("class:title", title))
    opts = flow.options()
    label_w = max((len(lbl) for _v, lbl, _h in opts), default=0) + 2
    for i, (_v, label, hint) in enumerate(opts):
        sel = i == flow.selected
        line(("class:cur" if sel else "class:name", ("▶ " if sel else "  ") + label.ljust(label_w)),
             ("class:dim", hint))
    if flow.error:
        line(("class:status", flow.error))
    line()
    for s in flow.summary():
        line(("class:live", s))
    line(("class:footer", OPTION_KEYS))
    return frags


def _render_picker(flow: NewFlow, width: int, height: int, line) -> None:
    banner = flow.banner()
    if banner:
        line(("class:dim", fit(banner, width)))
    elif flow.asking():
        line(("class:dim", "asking the model for a better place…"))
    else:
        line()
    head = [("class:title", flow.breadcrumb()), ("", "   ")]
    head += [("class:dim", "filter: "), ("", flow.filter), ("class:dim", "▏")] if flow.filter else \
        [("class:dim", PICKER_KEYS)]
    line(*head)
    visible = max(1, height - 7)
    top = max(0, min(flow.selected - visible + 1, len(flow.rows) - visible)) if flow.selected >= visible else 0
    styles = {"start": "class:live", "new": "class:title", "new_named": "class:title", "suggested": "class:status"}
    for i, (kind, label, _path) in enumerate(flow.rows[top:top + visible], top):
        sel = i == flow.selected
        text = fit(("▶ " if sel else "  ") + label, width)
        line(("class:cur" if sel else styles.get(kind, "class:name"), text))
    if not flow.rows:
        line(("class:dim", "no folder matches"))
    if flow.error:
        line(("class:status", flow.error))
