"""The new-session walk: a pure state machine (`NewFlow`) and its renderer (`render_new`).

Steps, in order: name → picker → folder name → kind (new folders) → confirm (wrapper, and GitHub for a new project,
preselected from `defaults`; `w` and `g` cycle them in place) → done. The picker
walks the tree one level at a time; each level lists up to three suggestion rows (the model's answer `★` once it
arrives, then the ranked alternatives `≈`), its child folders with session stats (most recent first, folded past
`FOLD_AT`), `● start here` (not at the root) and `+ new folder here…`. Typed text filters the children and is
offered as `+ new folder: <slug>`. Suggestions never move the picker or take the cursor; only a strong local answer
decides where the picker opens. Every step is written to `flow.log` (see `flowlog`). Nothing here touches the
terminal; all Index use stays on the calling (main) thread.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from prompt_toolkit.utils import get_cwidth

from .. import defaults, flowlog
from ..display import age, rel_or_abs
from ..index import FolderStats, Index
from ..launch import WRAPPERS, Launch, new_command
from ..paths import Env
from ..scaffold import Runner, check_name, create_folder
from ..suggest import Pending, Suggestion, default_http, log_outcome, suggest
from ..tree import Child, children, folder_stats_map, new_folder_kind_options, resolve_fragment, slug
from .resume import fit, wrap_items

_KIND_LABELS = {"project": ("a project", "start the session in it · git init · .claude-project · plan.md"),
                "container": ("a container", "a grouping folder only · then keep choosing inside it"),
                "client": ("a client", "writes a CLAUDE.md stub naming the client"),
                "organisational": ("organisational", "a grouping folder")}
_FOLDER_TEXT = {"project": "new project", "container": "new container", "client": "new client",
                "organisational": "new folder"}
_SEP = " › "
_NAME_COL = 28
FOLD_AT = 12  # a level with more children shows this many and a `… N more` row
MAX_SUGGESTIONS = 3
_SUGGESTION_KINDS = ("suggested", "alt")
PICKER_FOOTER = ("enter select · type to filter or name a new folder · backspace clear filter / up · esc top · "
                 "/path jump · ctrl-c cancel")
HIDDEN_HINT = "suggestions hidden while filtering · backspace to clear"
OPTION_KEYS = "↑↓ · enter choose · esc back · ctrl-c quit"
TEXT_KEYS = "enter confirm · esc back · ctrl-c quit"
CONFIRM_KEYS = "enter launch · w wrapper · g github · esc back · ctrl-c cancel"


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


def _recent_first(c: Child) -> tuple:
    """Children by last activity, newest first; folders with no session last, alphabetical."""
    last = c.stats.last_activity if c.stats.sessions else None
    return (last is None, -(last or 0.0), c.name.lower())


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
        self.pending: Pending | None = None
        self.suggestion: Suggestion | None = None  # the one whose folder was opened (shown as the banner there)
        self.offered: Suggestion | None = None  # the model's answer, shown as the ★ row
        self._model_applied = False
        self._preselect: str | None = None  # `+ new folder: <name>` from a suggestion, for self.folder only
        self._expanded: set[Path] = set()  # folded levels the user asked to see in full
        self._new_source: str | None = None  # the picker row kind that led to the folder-name step
        self._picker_key: tuple[str, str, str] | None = None  # the picker row last entered (restored by esc)
        self.new_name: str | None = None  # None: start here (no new folder)
        self.new_kind: str | None = None
        self.wrapper: str | None = None  # the confirm step's choices (preselected from `defaults`)
        self._confirm_bucket: str | None = None  # the bucket whose defaults the wrapper and GitHub values came from
        self.github: str | None = None  # personal | work | none; only a new project has one
        self.cwd: Path | None = None
        self.session_id: str | None = None
        self.warnings: list[str] = []

    # ---- name -------------------------------------------------------------------------------------------------------

    def start(self, name: str) -> NewFlow:
        """Begin the walk for session `name` (required). The picker opens at the root, or at the local answer when
        it is strong; the model stages run in the background (see `poll`)."""
        name = (name or "").strip()
        if not name:
            raise ValueError("a session name is required")
        self.name = name
        self.step = "picker"
        self.error = ""
        self.pending = self._suggest(self.env, self.index, name, http=self.http, runner=self.runner,
                                     on_update=lambda s: None)
        local = self.pending.local
        sure = local.strong and local.source != "root"
        self.suggestion = local if sure else None
        self._log("start", name=name, suggested=self._rel(local.path) if local.source != "root" else None,
                  source=local.source, strong=sure)
        self._open(local.path if sure else self.env.root, via="start")
        return self

    # ---- flow log ---------------------------------------------------------------------------------------------------

    def _rel(self, path: Path | None) -> str | None:
        if path is None:
            return None
        rel = rel_or_abs(self.env.root, path)
        return "" if rel == "." else rel

    def _log(self, event: str, kind: str | None = None, **extra) -> None:
        flowlog.log(self.env, event, folder=self._rel(self.folder), kind=kind, filter=self.filter, step=self.step,
                    ui="terminal", **extra)

    def cancel(self) -> None:
        """The user quit the walk (ctrl-c, or q on a choice step): logged only."""
        self._log("cancel")

    def suggestion_in_effect(self) -> Suggestion | None:
        """The model's answer once it arrived in the picker, else the local answer (None before `start`)."""
        if self.offered is not None:
            return self.offered
        return self.pending.local if self.pending is not None else None

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
        """Show the model's answer `s` as the ★ row at the top of every level. It never moves the picker and never
        takes the cursor. Ignored once the walk has left the picker."""
        if self.step != "picker":
            return
        self.offered = s
        self._refresh()

    def _open(self, folder: Path, preselect: str | None = None, via: str | None = None,
              want: tuple[str, str, str] | None = None) -> None:
        """Show `folder`'s level with an empty filter; `preselect` names a proposed new folder to select, `want` the
        key of another row to select."""
        self.folder = Path(folder)
        self.filter = ""
        self._preselect = preselect
        self._refresh(keep=False, want=want)
        self._log("open", kind=via)

    @staticmethod
    def _key(row: tuple[str, str, Path | None]) -> tuple[str, str, str]:
        """A row's identity across rebuilds: kind, path, and the proposed name of a `new_named` row (the label of
        `new_typed` follows the filter, so it is not part of its identity)."""
        kind, label, path = row
        if kind in _SUGGESTION_KINDS:
            kind = "suggestion"  # a ≈ row that becomes the ★ row for the same folder is the same row
        return kind, str(path).lower() if path is not None else "", label if kind == "new_named" else ""

    def _default_row(self) -> int:
        """Without a filter: the first row that is not a suggestion. While a filter is typed: the first matching
        child (or fragment match), else the typed new folder, else `+ new folder here…`; never `● start here`."""
        if not self.filter:
            return next((i for i, (k, _l, _p) in enumerate(self.rows) if k not in _SUGGESTION_KINDS), 0)
        for wanted in (("child", "match"), ("new_typed",), ("new",)):
            i = next((i for i, (k, _l, _p) in enumerate(self.rows) if k in wanted), None)
            if i is not None:
                return i
        return 0

    def _refresh(self, keep: bool = True, want: tuple[str, str, str] | None = None) -> None:
        """Rebuild the rows and choose the selection: `want` (a row key to restore) when present; else, with `keep`,
        the selected row by identity (a row arriving above it, such as a suggestion, never takes the cursor), except
        that `● start here` and the new-folder rows are never kept once a filter is typed (the typed text wins);
        else the proposed new folder on a freshly opened level; else `_default_row`."""
        current = self._key(self.rows[self.selected]) if keep and 0 <= self.selected < len(self.rows) else None
        self.rows = self._build_rows()
        keys = [self._key(r) for r in self.rows]
        kinds = [k for k, _l, _p in self.rows]
        if want is not None and want in keys:
            self.selected = keys.index(want)
        elif current in keys and not (self.filter and current[0] in ("start", "new", "new_named")):
            self.selected = keys.index(current)
        elif want is None and not keep and self._preselect and "new_named" in kinds:
            self.selected = kinds.index("new_named")
        else:
            self.selected = self._default_row()

    def _back_to_picker(self) -> None:
        """Return from a later step to the picker with the cursor on the row the user left it from."""
        self.step = "picker"
        self._refresh(keep=False, want=self._picker_key or ("", "", ""))

    def proposed_name(self) -> str | None:
        """A suggested new folder for this level: the one carried in by entering a ★ row, or the model's answer
        when it names this very folder."""
        if self._preselect:
            return self._preselect
        s = self.offered
        return s.new_folder if s is not None and s.new_folder and Path(s.path) == self.folder else None

    def _suggestion_rows(self) -> list[tuple[str, str, Path | None]]:
        """Up to `MAX_SUGGESTIONS` rows: the model's answer (★), then the ranked alternatives (≈): the model's
        alternatives when it gave some, otherwise the local top candidate and its alternatives. The current folder
        and duplicates (such as a local ≈ for the ★ folder) are left out."""
        rows: list[tuple[str, str, Path | None]] = []
        seen: set[str] = set()

        def add(kind: str, path: Path, label: str) -> None:
            key = str(path).lower()
            if key in seen:
                return
            seen.add(key)
            if Path(path) != self.folder:
                rows.append((kind, label, Path(path)))

        model = self.offered
        local = self.pending.local if self.pending is not None else None
        ranked = [local.path, *local.alternatives] if local is not None and local.source != "root" else []
        if model is not None:
            add("suggested", model.path, self._star_label(model))
            if model.alternatives:
                ranked = list(model.alternatives)
        for p in ranked:
            add("alt", p, f"≈ {rel_or_abs(self.env.root, p)}")
        return rows[:MAX_SUGGESTIONS]

    def _star_label(self, s: Suggestion) -> str:
        rel = rel_or_abs(self.env.root, s.path)
        if s.new_folder:
            return f"★ {rel} + new folder: {s.new_folder}"
        return f"★ {rel} + new folder…" if s.needs_name else f"★ {rel}"

    def suggestions_hidden(self) -> bool:
        """A filter is typed and suggestion rows would otherwise show at this level."""
        return bool(self.filter) and bool(self._suggestion_rows())

    def _build_rows(self) -> list[tuple[str, str, Path | None]]:
        typed = self.filter
        rows: list[tuple[str, str, Path | None]] = [] if typed else self._suggestion_rows()
        if "/" in typed:  # a fragment: `a/b` from this level, `/a/b` from the root
            base = self.env.root if typed.startswith("/") else self.folder
            for p in resolve_fragment(self.env, base, typed):
                rows.append(("match", self.crumb(p), p))
            return rows
        kids = sorted(children(self.env, self.index, self.folder, self._stats), key=_recent_first)
        more = 0
        new_typed = None
        if typed:
            needle, name = typed.lower(), slug(typed)
            kids = [c for c in kids if needle in c.name.lower() or (name and name in c.name.lower())]
            # prefix matches first, then other substring matches (each group stays most recent first)
            kids.sort(key=lambda c: not (c.name.lower().startswith(needle)
                                         or (name and c.name.lower().startswith(name))))
            if name and not self._name_problem(name):  # a usable name that does not exist here yet
                new_typed = ("new_typed", f"+ new folder: {name}", None)
        elif len(kids) > FOLD_AT and self.folder not in self._expanded:
            more = len(kids) - FOLD_AT
            kids = kids[:FOLD_AT]
        if new_typed and not kids:
            rows.append(new_typed)  # nothing matches: the typed text is the new folder, first and selected
        width = min(_NAME_COL, max((len(c.name) for c in kids), default=0))
        now = self.env.now()
        for c in kids:
            stats = _stats_text(c.stats, self._tool.get(tuple(p.lower() for p in c.path.parts)), now)
            rows.append(("child", f"{fit(c.name, width)}  {stats}", c.path))
        if more:
            rows.append(("more", f"… {more} more (type to filter, enter to show all)", None))
        if new_typed and kids:
            rows.append(new_typed)
        if self.folder != self.env.root:
            rows.append(("start", "● start here", None))
        proposed = self.proposed_name()
        if proposed and not (new_typed and slug(typed) == proposed):
            rows.append(("new_named", f"+ new folder: {proposed}", None))
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
        if text:  # every keystroke puts the cursor on the first match, not on a child that now sits lower
            self.rows = self._build_rows()
            self.selected = self._default_row()
        else:  # filter emptied: the level is back as it was, so the selected row stays by identity
            self._refresh()

    def backspace(self) -> None:
        """Delete from the filter; on an empty filter go up one level (not above the root)."""
        if self.filter:
            self.type_filter(self.filter[:-1])
        else:
            self.back()

    def back(self) -> None:
        """Up one level; at the root (with no filter) nothing happens."""
        self._act()
        if self.folder == self.env.root and not self.filter:
            return
        if self.folder != self.env.root and self.env.root in self.folder.parents:
            self._open(self.folder.parent, via="back")
        else:
            self._open(self.env.root, via="back")

    def esc(self) -> None:
        """Picker: back to the root (nothing to do at the root with no filter). Later steps: the step before."""
        if self.step in ("picker", "name"):
            if self.step == "picker":
                self._act()
                if self.folder != self.env.root or self.filter:
                    self._open(self.env.root, via="esc")
            return
        self.error = ""
        if self.step == "folder_name":
            self._back_to_picker()
        elif self.step == "kind":
            self._to_folder_name(self.new_name or "")
        elif self.step == "confirm":
            if self.new_name is None:  # start here
                self._back_to_picker()
            else:  # a new project: back to its kind
                self._to_kind()

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
        elif self.step == "kind":
            opts = self.options()
            if opts:
                self.choose_kind(opts[self.selected][0])
        elif self.step == "confirm":
            self._create()

    def _enter_picker(self) -> None:
        if not self.rows:
            return
        kind, _label, path = self.rows[self.selected]
        self._act()
        self._picker_key = self._key(self.rows[self.selected])  # for esc back from the later steps
        if self.filter:
            self._log("filter-enter", kind=kind)
        if kind in ("child", "match", "alt"):
            self._open(path, via=kind)
        elif kind == "suggested":
            s = self.suggestion = self.offered
            if s is not None and s.needs_name and not s.new_folder:  # a place without a name: ask for one
                self._open(path, via=kind, want=("new", "", ""))
            else:
                self._open(path, preselect=s.new_folder if s else None, via=kind)
        elif kind == "more":
            at = self.selected
            self._expanded.add(self.folder)
            self._refresh(keep=False)
            self.selected = min(at, len(self.rows) - 1)  # the first child that was folded away
        elif kind == "start":
            self.new_name = self.new_kind = None
            self._to_confirm()
        else:
            self._new_source = kind
            if kind == "new":
                self._to_folder_name(slug(self.name))
            elif kind == "new_named":
                self._to_folder_name(self.proposed_name() or slug(self.name))
            elif kind == "new_typed":
                self._to_folder_name(slug(self.filter))

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
        self._log("new-folder", kind=self._new_source, name=name)
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
        self._log("kind", value=kind, name=self.new_name)
        if kind == "project":
            self._to_confirm()
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

    def _to_confirm(self) -> None:
        """The one step before a launch: wrapper (and GitHub for a new project) preselected from the bucket's
        remembered choices; `w` and `g` change them."""
        bucket = defaults.bucket_key(self.env.root, self.target())
        d = defaults.get(self.env, self.target())
        self.step = "confirm"
        self.error = ""
        if bucket != self._confirm_bucket or self.wrapper is None:  # first entry, or another bucket: its defaults
            self.wrapper = d.wrapper
            self.github = None
        self._confirm_bucket = bucket  # the same bucket after esc and back keeps what `w` and `g` chose
        if not self._new_project():
            self.github = None
        elif self.github is None:
            self.github = d.github

    def _new_project(self) -> bool:
        return bool(self.new_name) and self.new_kind == "project"

    def _wrapper_options(self) -> list[tuple[str, str, str]]:
        # the Claude wrappers, then qwen as the other tool
        return [(w, w, "") for w in WRAPPERS if w != "qwen"] + [("qwen", "qwen", "the Qwen Code tool")]

    def choose_wrapper(self, wrapper: str) -> None:
        """Set the wrapper on the confirm step (the launch happens on enter)."""
        if wrapper not in WRAPPERS:
            raise ValueError(f"unknown wrapper: {wrapper!r}")
        self.wrapper = wrapper

    def choose_github(self, account: str) -> None:
        """Set the GitHub choice on the confirm step: personal, work or none (git init only)."""
        if account not in defaults.GITHUB_CHOICES:
            raise ValueError(f"unknown GitHub account: {account!r}")
        self.github = account

    def cycle_wrapper(self) -> None:
        """`w`: the next wrapper, in the order of the old wrapper step."""
        values = [v for v, _l, _h in self._wrapper_options()]
        now = values.index(self.wrapper) if self.wrapper in values else -1
        self.wrapper = values[(now + 1) % len(values)]

    def cycle_github(self) -> None:
        """`g`: personal -> work -> none -> personal; only for a new project (otherwise nothing is created)."""
        if not self._new_project():
            return
        values = defaults.GITHUB_CHOICES
        now = values.index(self.github) if self.github in values else -1
        self.github = values[(now + 1) % len(values)]

    def creating(self) -> Path | None:
        """The folder `enter` is about to create on this step (so the app can say so before git/gh block it)."""
        if self.step == "confirm" and self.new_name:
            return self.target()
        if self.step == "kind" and self.options() and self.options()[self.selected][0] != "project":
            return self.target()
        if self.step == "folder_name" and self._kind_values() == ["organisational"] \
                and not self._name_problem(self.text.strip()):
            return self.folder / self.text.strip()
        return None

    def _create_new(self) -> Path | None:
        """`create_folder` for the pending new folder; on failure back to the folder-name step with the error."""
        try:
            created = create_folder(self.folder, self.new_name, self.new_kind, self.name, runner=self.runner,
                                    github=self.github if self.github in ("personal", "work") else None)
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
        try:
            defaults.remember(self.env, self.cwd, self.wrapper, self.github if self._new_project() else None)
        except (OSError, ValueError) as e:
            self.warnings.append(f"could not remember the choices: {e}")
        self._log("launch", folder_kind=self.new_kind, new_folder=self.new_name, wrapper=self.wrapper, github=self.github,
                  cwd=self._rel(self.cwd))
        log_outcome(self.env, self.name, self.suggestion_in_effect(), self.folder, self.new_name)

    def result(self) -> Launch | None:
        if self.step != "done":
            return None
        launch = new_command(self.cwd, self.name, self.wrapper, session_id=self.session_id)
        launch.warnings.extend(self.warnings)
        return launch

    # ---- option steps -----------------------------------------------------------------------------------------------

    def options(self) -> list[tuple[str, str, str]]:
        """(value, label, hint) for the kind step; [] elsewhere."""
        if self.step == "kind":
            return [(k, *_KIND_LABELS[k]) for k in self._kind_values()]
        return []

    def command_text(self) -> str:
        """`cd <folder> ; <wrapper> -n "<name>" ...`: exactly what the launch runs (same flags as `new_command`)."""
        argv = new_command(self.target(), self.name, self.wrapper or "").argv
        run = " ".join(f'"{a}"' if a == self.name and i == 2 else a for i, a in enumerate(argv))
        return f"cd {self.target()} ; {run}"

    def summary(self) -> list[str]:
        """The `→ mkdir …` / `→ cd … ; <wrapper> -n "…"` lines for what will run."""
        lines = []
        if self.new_name:
            extra = " ; git init ; plan.md" if self.new_kind == "project" else ""
            if self.new_kind == "project" and self.github in ("personal", "work"):
                extra += f" ; GitHub ({self.github})"
            lines.append(f"→ mkdir {self.target()}{extra}")
        if self.step in ("confirm", "done"):
            lines.append(f"→ {self.command_text()}")
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
        _footer(line, TEXT_KEYS, width)
        return frags

    line(("class:title", "Session name: "), ("", flow.name))
    line()
    if flow.step == "picker":
        _render_picker(flow, width, height, line)
        return frags
    new_folder = flow.new_name is not None or flow.step in ("folder_name", "kind")
    if flow.step == "confirm":
        _render_confirm(flow, width, line)
        return frags
    line(("class:title", flow.breadcrumb()), ("class:dim", "  ·  new folder here" if new_folder else "  ·  start here"))
    line()
    if flow.step == "folder_name":
        line(("class:title", "Folder name: "), ("", flow.text), ("class:dim", "▏"))
        if flow.error:
            line(("class:status", flow.error))
        line()
        _footer(line, TEXT_KEYS, width)
        return frags
    if flow.new_name and flow.step != "kind":
        line(("class:title", "Folder name: "), ("", flow.new_name), ("class:dim", f"  ({flow.new_kind})"))
    line(("class:title", "What is it?"))
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
    _footer(line, OPTION_KEYS, width)
    return frags


def _footer(line, keys: str, width: int) -> None:
    """The key help wrapped between items to `width`, as the picker footer is."""
    for text in wrap_items(keys, max(1, width)):
        line(("class:footer", text))


def _hanging(text: str, width: int, indent: int) -> list[str]:
    """`text` cut into lines of at most `width` cells; the continuation lines are indented by `indent`."""
    out: list[str] = []
    cur, used = " " * indent, indent
    for ch in text:
        w = get_cwidth(ch)
        if used + w > width:
            out.append(cur)
            cur, used = " " * indent, indent
        cur += ch
        used += w
    out.append(cur)
    return out


def _render_confirm(flow: NewFlow, width: int, line) -> None:
    """The confirm step: where, what is created, wrapper, GitHub (new project only) and the exact command."""
    line(("class:title", flow.crumb(flow.target())))
    new = flow.new_name is not None
    what = _FOLDER_TEXT.get(flow.new_kind or "", "new folder") if new else "existing"
    line(("class:dim", "folder   "), ("class:name", what), ("", f"   {flow.new_name}" if new else ""))
    line(("class:dim", "wrapper  "), ("class:name", flow.wrapper or ""), ("class:dim", "   (w to change)"))
    if flow.new_kind == "project" and new:
        line(("class:dim", "github   "), ("class:name", flow.github or "none"), ("class:dim", "   (g to change)"))
    for n, text in enumerate(_hanging(flow.command_text(), max(10, width), 9)):
        line(*([("class:dim", "runs     ")] if n == 0 else []), ("class:live", text[9:] if n == 0 else text))
    if flow.error:
        line(("class:status", flow.error))
    line()
    _footer(line, CONFIRM_KEYS, width)


def _render_picker(flow: NewFlow, width: int, height: int, line) -> None:
    banner = flow.banner()
    if banner:
        line(("class:dim", fit(banner, width)))
    elif flow.asking():
        line(("class:dim", "asking the model for a better place…"))
    else:
        line()
    head = [("class:title", flow.breadcrumb())]
    if flow.filter:
        head += [("", "   "), ("class:dim", "filter: "), ("", flow.filter), ("class:dim", "▏")]
    line(*head)
    hidden = flow.suggestions_hidden()
    if hidden:
        line(("class:dim", fit(HIDDEN_HINT, width).rstrip()))
    footer = wrap_items(PICKER_FOOTER, max(1, width))
    chrome = 6 + len(footer) + (1 if flow.status else 0) + (1 if flow.error else 0) + (1 if hidden else 0)
    visible = max(1, height - chrome)
    top = max(0, min(flow.selected - visible + 1, len(flow.rows) - visible)) if flow.selected >= visible else 0
    styles = {"start": "class:live", "new": "class:title", "new_named": "class:title", "new_typed": "class:title",
              "suggested": "class:status", "alt": "class:section", "more": "class:dim"}
    for i, (kind, label, _path) in enumerate(flow.rows[top:top + visible], top):
        sel = i == flow.selected
        text = fit(("▶ " if sel else "  ") + label, width)
        line(("class:cur" if sel else styles.get(kind, "class:name"), text))
    if not flow.rows:
        line(("class:dim", "no folder matches"))
    if flow.error:
        line(("class:status", flow.error))
    line()
    for text in footer:
        line(("class:footer", text))
