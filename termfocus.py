"""Terminalfenster einer Claude-Code-Session nach vorn holen und ihren Tab waehlen.

Gebraucht, wenn man auf dem Clawdmeter lange auf den Clawd-Bildschirm drueckt
(REQ 0x05): das Fenster dieser Session kommt nach vorn, in Windows Terminal
auch mit dem richtigen Tab.

Ablauf je Anfrage (focus_session):
1. Fenster finden: EnumWindows liefert die Konsolenfenster (PseudoConsoleWindow
   von ConPTY/Windows Terminal, ConsoleWindowClass vom klassischen conhost).
   GetWindowThreadProcessId nennt bei ihnen den ersten Prozess der Konsole
   (meist die Shell). Der wird mit der Session-PID und ihren Vorfahren
   verglichen, der naechste Treffer gilt. GetAncestor(GA_ROOTOWNER) ergibt das
   echte Fenster (in Windows Terminal CASCADIA_HOSTING_WINDOW_CLASS).
   Ersatzweise AttachConsole(pid) + GetConsoleWindow.
   VS Code und andere Programme: nur deren Fenster (bei VS Code das, dessen
   Titel den Ordnernamen traegt), kein Tab.
2. Tab waehlen (nur Windows Terminal mit mehreren Konsolen im Fenster): per UI
   Automation ueber pythonnet (liegt fuer pywebview ohnehin bei). Reihenfolge:
   genauer Konsolentitel -> derselbe ohne fuehrendes Statuszeichen -> kurz ein
   Merker als Titel ("CSB-<zufall>"), den Tab mit diesem Namen suchen, Titel
   zurueck -> urspruenglicher Konsolentitel -> kein Tabwechsel.
3. Nach vorn: ShowWindow(SW_RESTORE) nur wenn minimiert, SetForegroundWindow;
   klappt das nicht, ALT antippen und nochmal, dann AttachThreadInput.

Kosten: nichts laeuft ohne Anfrage - kein Thread, kein Timer, kein Polling.
pythonnet/UIA wird erst bei der ersten Anfrage geladen, die es braucht, und
dann wiederverwendet. Jede Anfrage hat ein festes Zeitbudget (BUDGET_S) und
Wiederholungen innerhalb von REPEAT_S werden verworfen.

AttachConsole: nur in einem Prozess ohne eigene Konsole (die App laeuft unter
pythonw.exe). Waehrend er an einer fremden Konsole haengt, ignoriert der
Prozess Ctrl+C/Break; die Zeit dort ist kurz (nur Titel lesen/setzen), danach
sofort FreeConsole und die alten Standard-Handles zurueck. Kein Unterprozess
aus diesem Modul, solange es angehaengt ist.

Zum Ausprobieren:  python termfocus.py --list   |   python termfocus.py <pid>
"""

import contextlib
import ctypes
import os
import re
import sys
import threading
import time
import uuid
from ctypes import wintypes as W

BUDGET_S = 1.5          # hartes Zeitbudget je Anfrage
REPEAT_S = 1.0          # Anfragen so kurz nach der letzten werden verworfen
MARKER_WAIT_S = 0.5     # so lange auf den Tab mit dem Merker-Titel warten
FOCUS_RESERVE_S = 0.25  # so viel vom Budget bleibt fuer das Nach-vorn-Holen
POLL_S = 0.04

PSEUDO_CLASS = "PseudoConsoleWindow"
CONHOST_CLASS = "ConsoleWindowClass"
WT_CLASS = "CASCADIA_HOSTING_WINDOW_CLASS"

# Vorfahren, deren Fenster nie "das Terminal" sind (Explorer, Shells, Dienste)
# - und Windows Terminal selbst: ohne Konsolen-Zuordnung waere jedes seiner
# Fenster geraten.
_NOT_HOSTS = {
    "explorer.exe", "cmd.exe", "powershell.exe", "pwsh.exe", "conhost.exe",
    "openconsole.exe", "bash.exe", "wsl.exe", "wslhost.exe", "svchost.exe",
    "services.exe", "wininit.exe", "winlogon.exe", "userinit.exe", "sihost.exe",
    "runtimebroker.exe", "windowsterminal.exe", "wt.exe", "claude.exe", "node.exe",
}

# Fuehrendes Statuszeichen im Titel, das Claude Code setzt ("✳ Name",
# Spinner "◐ Name"): alles vor dem ersten Buchstaben/Ziffer.
_GLYPH = re.compile(r"^[^\w]+", re.UNICODE)


def _strip(title: str) -> str:
    return _GLYPH.sub("", str(title or "")).strip()


# -- Win32 (ctypes) -----------------------------------------------------------

class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", W.WORD), ("wScan", W.WORD), ("dwFlags", W.DWORD),
                ("time", W.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", W.LONG), ("dy", W.LONG), ("mouseData", W.DWORD),
                ("dwFlags", W.DWORD), ("time", W.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", W.DWORD), ("u", _INPUTUNION)]


class _Win32:
    """Alle Fenster- und Konsolen-Aufrufe an einem Ort (Tests ersetzen ihn).
    Eigene WinDLL-Instanzen: die argtypes hier aendern ctypes.windll nicht."""

    GA_ROOTOWNER = 3
    GW_OWNER = 4
    SW_RESTORE = 9
    VK_MENU = 0x12
    KEYEVENTF_KEYUP = 0x2
    STD_HANDLES = (-10, -11, -12)

    def __init__(self):
        u = self.u = ctypes.WinDLL("user32", use_last_error=True)
        k = self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        self._enum_proc = ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
        u.EnumWindows.argtypes = [self._enum_proc, W.LPARAM]
        u.GetWindowThreadProcessId.argtypes = [W.HWND, ctypes.POINTER(W.DWORD)]
        u.GetWindowThreadProcessId.restype = W.DWORD
        u.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, ctypes.c_int]
        u.GetClassNameW.argtypes = [W.HWND, W.LPWSTR, ctypes.c_int]
        u.IsWindowVisible.argtypes = [W.HWND]
        u.IsIconic.argtypes = [W.HWND]
        u.GetAncestor.argtypes = [W.HWND, W.UINT]
        u.GetAncestor.restype = W.HWND
        u.GetWindow.argtypes = [W.HWND, W.UINT]
        u.GetWindow.restype = W.HWND
        u.ShowWindow.argtypes = [W.HWND, ctypes.c_int]
        u.SetForegroundWindow.argtypes = [W.HWND]
        u.BringWindowToTop.argtypes = [W.HWND]
        u.GetForegroundWindow.restype = W.HWND
        u.AttachThreadInput.argtypes = [W.DWORD, W.DWORD, W.BOOL]
        u.SendInput.argtypes = [W.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
        u.SendInput.restype = W.UINT
        k.GetConsoleWindow.restype = W.HWND
        k.AttachConsole.argtypes = [W.DWORD]
        k.GetConsoleProcessList.argtypes = [ctypes.POINTER(W.DWORD), W.DWORD]
        k.GetConsoleProcessList.restype = W.DWORD
        k.GetConsoleTitleW.argtypes = [W.LPWSTR, W.DWORD]
        k.GetConsoleOriginalTitleW.argtypes = [W.LPWSTR, W.DWORD]
        k.SetConsoleTitleW.argtypes = [W.LPCWSTR]
        k.GetStdHandle.argtypes = [W.DWORD]
        k.GetStdHandle.restype = W.HANDLE
        k.SetStdHandle.argtypes = [W.DWORD, W.HANDLE]
        k.GetCurrentThreadId.restype = W.DWORD
        self._ctrl_proto = ctypes.WINFUNCTYPE(W.BOOL, W.DWORD)
        k.SetConsoleCtrlHandler.argtypes = [ctypes.c_void_p, W.BOOL]
        self._ctrl_handler = None

    # Fenster
    def console_windows(self) -> list:
        """[(hwnd, klasse, pid des ersten Konsolenprozesses)] aller Konsolenfenster."""
        out = []

        def cb(h, _):
            cls = self.class_name(h)
            if cls in (PSEUDO_CLASS, CONHOST_CLASS):
                out.append((h, cls, self.window_pid(h)))
            return True
        self.u.EnumWindows(self._enum_proc(cb), 0)
        return out

    def top_windows(self, pids) -> list:
        """[(hwnd, pid, titel)] sichtbarer, betitelter, unbesessener Fenster
        dieser Prozesse, in Z-Reihenfolge (oberstes zuerst)."""
        pids = set(pids)
        out = []

        def cb(h, _):
            if self.u.IsWindowVisible(h) and not self.u.GetWindow(h, self.GW_OWNER):
                pid = self.window_pid(h)
                if pid in pids:
                    text = self.title(h)
                    if text:
                        out.append((h, pid, text))
            return True
        self.u.EnumWindows(self._enum_proc(cb), 0)
        return out

    def window_pid(self, h) -> int:
        pid = W.DWORD()
        self.u.GetWindowThreadProcessId(h, ctypes.byref(pid))
        return pid.value

    def class_name(self, h) -> str:
        b = ctypes.create_unicode_buffer(256)
        self.u.GetClassNameW(h, b, 256)
        return b.value

    def title(self, h) -> str:
        b = ctypes.create_unicode_buffer(512)
        self.u.GetWindowTextW(h, b, 512)
        return b.value

    def root_owner(self, h) -> int:
        return self.u.GetAncestor(h, self.GA_ROOTOWNER) or 0

    def is_visible(self, h) -> bool:
        return bool(self.u.IsWindowVisible(h))

    # Konsole (AttachConsole)
    def _guard_ctrl(self) -> None:
        """Einmal einen eigenen Handler eintragen: Ctrl+C/Break/Schliessen der
        fremden Konsole duerfen diesen Prozess nicht beenden. Der Prozess hat
        sonst keine Konsole, also bekommt er ausserhalb davon nie etwas."""
        if self._ctrl_handler is None:
            self._ctrl_handler = self._ctrl_proto(lambda evt: evt in (0, 1, 2))
            self.k.SetConsoleCtrlHandler(
                ctypes.cast(self._ctrl_handler, ctypes.c_void_p), True)

    @contextlib.contextmanager
    def attached(self, pid: int):
        with _console_lock:
            buf = (W.DWORD * 1)()
            if self.k.GetConsoleProcessList(buf, 1):
                raise OSError("this process has its own console")
            self._guard_ctrl()
            saved = [self.k.GetStdHandle(n) for n in self.STD_HANDLES]
            self.k.SetConsoleCtrlHandler(None, True)   # NULL: Ctrl+C ignorieren
            try:
                if not self.k.AttachConsole(pid):
                    raise ctypes.WinError(ctypes.get_last_error())
                try:
                    yield self
                finally:
                    self.k.FreeConsole()
                    for n, hd in zip(self.STD_HANDLES, saved):
                        self.k.SetStdHandle(n, hd)
            finally:
                # Das Ignorieren erben sonst spaeter gestartete Programme.
                self.k.SetConsoleCtrlHandler(None, False)

    def _console_title(self, original=False) -> str:
        b = ctypes.create_unicode_buffer(1024)
        (self.k.GetConsoleOriginalTitleW if original else self.k.GetConsoleTitleW)(b, 1024)
        return b.value

    def console_info(self, pid: int) -> dict:
        """Konsolenfenster, Titel und urspruenglicher Titel der Konsole von pid."""
        with self.attached(pid):
            return {"hwnd": self.k.GetConsoleWindow() or 0,
                    "title": self._console_title(),
                    "original": self._console_title(original=True)}

    def swap_title(self, pid: int, marker: str) -> str:
        """Titel durch marker ersetzen; liefert den alten Titel."""
        with self.attached(pid):
            saved = self._console_title()
            self.k.SetConsoleTitleW(marker)
            return saved

    def restore_title(self, pid: int, marker: str, saved: str) -> bool:
        """Alten Titel zurueck - nur wenn noch der Merker drinsteht (sonst hat
        das Programm inzwischen selbst einen gesetzt)."""
        with self.attached(pid):
            if self._console_title() != marker:
                return False
            self.k.SetConsoleTitleW(saved)
            return True

    # Vordergrund
    def is_iconic(self, h) -> bool:
        return bool(self.u.IsIconic(h))

    def restore(self, h) -> None:
        self.u.ShowWindow(h, self.SW_RESTORE)

    def foreground(self) -> int:
        return self.u.GetForegroundWindow() or 0

    def set_foreground(self, h) -> bool:
        return bool(self.u.SetForegroundWindow(h))

    def alt_tap(self) -> None:
        """ALT einmal druecken und loslassen: hebt die Vordergrund-Sperre auf."""
        arr = (_INPUT * 2)()
        for i, flags in enumerate((0, self.KEYEVENTF_KEYUP)):
            arr[i].type = 1          # INPUT_KEYBOARD
            arr[i].u.ki = _KEYBDINPUT(self.VK_MENU, 0, flags, 0, 0)
        self.u.SendInput(2, arr, ctypes.sizeof(_INPUT))

    def set_foreground_attached(self, h) -> bool:
        """SetForegroundWindow mit an den Vordergrund-Thread gehaengter Eingabe."""
        fg = self.foreground()
        tid = self.u.GetWindowThreadProcessId(fg, None) if fg else 0
        me = self.k.GetCurrentThreadId()
        hooked = bool(tid and tid != me and self.u.AttachThreadInput(me, tid, True))
        try:
            self.u.BringWindowToTop(h)
            return bool(self.u.SetForegroundWindow(h))
        finally:
            if hooked:
                self.u.AttachThreadInput(me, tid, False)


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", W.DWORD), ("cntUsage", W.DWORD), ("th32ProcessID", W.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", W.DWORD),
                ("cntThreads", W.DWORD), ("th32ParentProcessID", W.DWORD),
                ("pcPriClassBase", W.LONG), ("dwFlags", W.DWORD), ("szExeFile", W.WCHAR * 260)]


class _Procs:
    """Prozess-Abfragen (Tests ersetzen sie). Die Vorfahren kommen aus einem
    einzigen Toolhelp-Schnappschuss: psutil.parent() macht je Stufe einen
    eigenen und braucht so ~250 ms."""

    def __init__(self):
        self._k = None

    def _kernel(self):
        if self._k is None:
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            k.CreateToolhelp32Snapshot.argtypes = [W.DWORD, W.DWORD]
            k.CreateToolhelp32Snapshot.restype = W.HANDLE
            k.Process32FirstW.argtypes = [W.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
            k.Process32NextW.argtypes = [W.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
            k.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
            k.OpenProcess.restype = W.HANDLE
            k.GetProcessTimes.argtypes = [W.HANDLE] + [ctypes.POINTER(W.FILETIME)] * 4
            k.CloseHandle.argtypes = [W.HANDLE]
            self._k = k
        return self._k

    def _table(self) -> dict:
        """{pid: (eltern-pid, exe-name)} aller Prozesse."""
        k = self._kernel()
        snap = k.CreateToolhelp32Snapshot(0x2, 0)          # TH32CS_SNAPPROCESS
        if not snap or snap == W.HANDLE(-1).value:
            return {}
        out = {}
        try:
            e = _PROCESSENTRY32W()
            e.dwSize = ctypes.sizeof(e)
            ok = k.Process32FirstW(snap, ctypes.byref(e))
            while ok:
                out[e.th32ProcessID] = (e.th32ParentProcessID, e.szExeFile)
                ok = k.Process32NextW(snap, ctypes.byref(e))
        finally:
            k.CloseHandle(snap)
        return out

    def _created(self, pid: int):
        """Startzeit (FILETIME als int) oder None, wenn nicht lesbar."""
        k = self._kernel()
        h = k.OpenProcess(0x1000, False, pid)              # QUERY_LIMITED_INFORMATION
        if not h:
            return None
        try:
            c, x, kt, ut = W.FILETIME(), W.FILETIME(), W.FILETIME(), W.FILETIME()
            if not k.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(x),
                                     ctypes.byref(kt), ctypes.byref(ut)):
                return None
            return (c.dwHighDateTime << 32) | c.dwLowDateTime
        finally:
            k.CloseHandle(h)

    def ancestors(self, pid: int) -> list:
        """[(pid, name)] von pid selbst bis zur Wurzel. Ein Elternteil, das
        nach dem Kind gestartet ist, ist eine wiederverwendete PID: dort Schluss."""
        table = self._table()
        out, seen = [], set()
        cur, born = pid, self._created(pid)
        while cur and cur not in seen and len(out) < 32:
            seen.add(cur)
            parent, name = table.get(cur, (0, ""))
            out.append((cur, name))
            if not parent or parent not in table:
                break
            pborn = self._created(parent)
            if born is not None and pborn is not None and pborn > born:
                break
            cur, born = parent, pborn
        return out or [(pid, "")]

    def cwd(self, pid: int) -> str:
        import psutil
        try:
            return psutil.Process(pid).cwd()
        except psutil.Error:
            return ""


class _Uia:
    """UI Automation ueber pythonnet. Wird erst bei Bedarf gebaut (Laden der
    .NET-Assemblies) und dann wiederverwendet."""

    _KEY = ", Version=4.0.0.0, Culture=neutral, PublicKeyToken=31bf3856ad364e35"

    def __init__(self):
        import clr
        clr.AddReference("UIAutomationClient" + self._KEY)
        clr.AddReference("UIAutomationTypes" + self._KEY)
        from System import IntPtr
        from System.Windows.Automation import (AutomationElement, ControlType,
                                               PropertyCondition, SelectionItemPattern,
                                               TreeScope)
        self._ptr = IntPtr
        self._ae = AutomationElement
        self._scope = TreeScope.Descendants
        self._sip = SelectionItemPattern
        self._cond = PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.TabItem)

    def tabs(self, hwnd: int) -> list:
        el = self._ae.FromHandle(self._ptr(int(hwnd)))
        found = el.FindAll(self._scope, self._cond)
        return [_Tab(found[i], self._sip) for i in range(found.Count)]


class _Tab:
    def __init__(self, el, sip):
        self._el, self._sip = el, sip
        self.name = str(el.Current.Name or "")

    def selected(self) -> bool:
        return bool(self._el.GetCurrentPattern(self._sip.Pattern).Current.IsSelected)

    def select(self) -> bool:
        self._el.GetCurrentPattern(self._sip.Pattern).Select()
        return True


_console_lock = threading.Lock()   # AttachConsole ist prozessweit
_gate = threading.Lock()           # eine Anfrage zur Zeit
_last_at = None                    # monotonic() der letzten angenommenen Anfrage
_win_obj = None
_procs = _Procs()
_uia_obj = None
_uia_error = None                  # Laden schon einmal gescheitert: nicht jedes Mal neu
_uia_lock = threading.Lock()


def _win() -> _Win32:
    global _win_obj
    if _win_obj is None:
        _win_obj = _Win32()
    return _win_obj


def _uia():
    """Das UIA-Objekt, beim ersten Mal geladen; None wenn es nicht geht."""
    global _uia_obj, _uia_error
    with _uia_lock:
        if _uia_obj is None and _uia_error is None:
            try:
                _uia_obj = _Uia()
            except Exception as e:
                _uia_error = f"{type(e).__name__}: {e}"
        return _uia_obj


# -- Fenster zur Session finden ----------------------------------------------

def _resolve(pid: int, cwd: str | None = None) -> dict:
    """Fenster zur Session-PID. {"host": hwnd oder 0, "kind": "wt" | "conhost" |
    "other" | "", "console": hwnd, "via": wie gefunden, "consoles": Anzahl
    Konsolen im Host-Fenster (Windows Terminal: Tabs/Bereiche)}."""
    w = _win()
    chain = _procs.ancestors(pid)
    consoles = w.console_windows()
    owners = {}
    for h, cls, cpid in consoles:
        owners.setdefault(cpid, []).append(h)
    console, via = 0, ""
    for p, name in chain:                      # naechster Vorfahr zuerst
        if p in owners:
            console, via = owners[p][0], f"{name or '?'}({p})"
            break
    if not console:
        try:
            console = w.console_info(pid)["hwnd"]
            via = "attach"
        except OSError:
            console = 0
    out = {"host": 0, "kind": "", "console": console, "via": via, "consoles": 0}
    if console:
        root = w.root_owner(console) or console
        rcls = w.class_name(root)
        if rcls == WT_CLASS:
            n = sum(1 for h, cls, _ in consoles
                    if cls == PSEUDO_CLASS and (w.root_owner(h) or h) == root)
            out.update(host=root, kind="wt", consoles=max(1, n))
            return out
        if rcls == CONHOST_CLASS:
            out.update(host=root, kind="conhost", consoles=1)
            return out
        if root != console and rcls != PSEUDO_CLASS and w.is_visible(root):
            out.update(host=root, kind="other", consoles=1)
            return out
    # Kein Terminal-Fenster (VS Code & Co. setzen keinen Besitzer): das Fenster
    # des naechsten Vorfahren, das ein Programmfenster hat.
    folder = os.path.basename(str(cwd or _procs.cwd(pid) or "").rstrip("\\/")).lower()
    for p, name in chain[1:]:
        if (name or "").lower() in _NOT_HOSTS:
            continue
        wins = w.top_windows([p])
        if not wins:
            continue
        hit = [h for h, _, text in wins if folder and folder in text.lower()]
        out.update(host=(hit or [wins[0][0]])[0], kind="other", consoles=1,
                   via=f"{name or '?'}({p})" + ("" if hit else " no title match"))
        return out
    return out


# -- Tab waehlen ---------------------------------------------------------------

def _unique(tabs, want, key):
    if not key(want):
        return None
    hits = [t for t in tabs if key(t.name) == key(want)]
    return hits[0] if len(hits) == 1 else None


def _match(tabs, title, original):
    """Tab ohne Merker: (tab, art) oder (None, "")."""
    for art, want, key in (("exact", title, lambda s: s),
                           ("stripped", title, _strip),
                           ("original", original, lambda s: s)):
        tab = _unique(tabs, want, key)
        if tab is not None:
            return tab, art
    return None, ""


def _select_tab(pid: int, host: int, deadline: float, notes: list) -> str:
    w = _win()
    try:
        info = w.console_info(pid)
    except OSError as e:
        notes.append(f"console: {e}")
        return "none"                         # z.B. erhoeht laufend
    if time.monotonic() > deadline - FOCUS_RESERVE_S:
        notes.append("budget")
        return "none"
    uia = _uia()
    if uia is None:
        notes.append(f"uia: {_uia_error}")
        return "none"
    try:
        tabs = uia.tabs(host)
        if len(tabs) <= 1:
            return "single"
        title, original = info["title"], info["original"]
        for art, want, key in (("exact", title, lambda s: s), ("stripped", title, _strip)):
            tab = _unique(tabs, want, key)
            if tab is not None:
                tab.select()
                return art
        if _marker_select(pid, host, uia, deadline, notes):
            return "marker"
        if time.monotonic() < deadline - FOCUS_RESERVE_S:
            tab = _unique(uia.tabs(host), original, lambda s: s)
            if tab is not None:
                tab.select()
                return "original"
    except Exception as e:
        notes.append(f"uia: {type(e).__name__}: {e}")
    return "none"


def _marker_select(pid, host, uia, deadline, notes) -> bool:
    """Kurz einen eindeutigen Titel setzen, den Tab mit diesem Namen waehlen,
    alten Titel zurueck. Angehaengt nur zum Setzen und Zuruecksetzen - beim
    Warten auf UIA nicht."""
    stop = min(deadline - FOCUS_RESERVE_S, time.monotonic() + MARKER_WAIT_S)
    if time.monotonic() >= stop:
        notes.append("budget")
        return False
    marker = "CSB-" + uuid.uuid4().hex[:8]
    w = _win()
    try:
        saved = w.swap_title(pid, marker)
    except OSError as e:
        notes.append(f"marker: {e}")
        return False
    try:
        while True:
            tab = _unique(uia.tabs(host), marker, lambda s: s)
            if tab is not None:
                tab.select()
                return True
            if time.monotonic() + POLL_S >= stop:
                notes.append("marker not seen")
                return False
            time.sleep(POLL_S)
    finally:
        try:
            if not w.restore_title(pid, marker, saved):
                notes.append("title changed meanwhile")
        except OSError as e:
            notes.append(f"title restore: {e}")


# -- Nach vorn -------------------------------------------------------------------

def _wait_foreground(h, until: float) -> bool:
    w = _win()
    while True:
        if w.foreground() == h:
            return True
        if time.monotonic() >= until:
            return False
        time.sleep(0.02)


def _bring_to_front(h: int, deadline: float) -> str:
    """Fenster in den Vordergrund; liefert den Weg ("plain", "alt", "attach")
    oder "" wenn es nicht geklappt hat."""
    w = _win()
    if w.is_iconic(h):
        w.restore(h)          # nur dann: SW_RESTORE wuerde sonst ein maximiertes verkleinern
    for step in ("plain", "alt", "attach"):
        if step == "alt":
            w.alt_tap()
        if step == "attach":
            w.set_foreground_attached(h)
        else:
            w.set_foreground(h)
        if _wait_foreground(h, min(deadline, time.monotonic() + 0.12)):
            return step
        if time.monotonic() >= deadline:
            break
    return ""


# -- oeffentlich ---------------------------------------------------------------

def focus_session(pid: int, cwd: str | None = None) -> dict:
    """Fenster (und Tab) der Session mit dieser PID nach vorn holen.

    {"ok": Fenster ist vorn, "window": dito, "tab": "exact" | "stripped" |
    "marker" | "original" | "none" | "single", "via": Weg nach vorn, "ms":
    Dauer, "error": Grund wenn nicht ok, "note": Hinweise}. Wirft nie."""
    global _last_at
    t0 = time.monotonic()
    res = {"ok": False, "window": False, "tab": "none", "via": "", "ms": 0,
           "error": "", "note": ""}
    if not _gate.acquire(blocking=False):
        res["error"] = "busy"
        return res
    try:
        if _last_at is not None and t0 - _last_at < REPEAT_S:
            res["error"] = "repeat ignored"
            return res
        _last_at = t0
        deadline = t0 + BUDGET_S
        notes = []
        try:
            target = _resolve(int(pid), cwd)
            host = target["host"]
            if not host:
                res["error"] = "no window found"
            else:
                if target["kind"] == "wt" and target["consoles"] > 1:
                    res["tab"] = _select_tab(int(pid), host, deadline, notes)
                elif target["kind"] in ("wt", "conhost"):
                    res["tab"] = "single"
                via = _bring_to_front(host, deadline)
                res.update(ok=bool(via), window=bool(via), via=via)
                if not via:
                    res["error"] = "window did not come to the front"
        except Exception as e:
            res["error"] = f"{type(e).__name__}: {e}"
        res["note"] = "; ".join(notes)
        return res
    finally:
        res["ms"] = int((time.monotonic() - t0) * 1000)
        _gate.release()


def describe(pid: int, cwd: str | None = None) -> dict:
    """Nur lesen (fuer --list): Fenster, Titel, Tabs und welcher Tab gewaehlt
    wuerde - ohne Merker, ohne etwas nach vorn zu holen."""
    w = _win()
    d = _resolve(int(pid), cwd)
    d["host_class"] = w.class_name(d["host"]) if d["host"] else ""
    d["host_title"] = w.title(d["host"]) if d["host"] else ""
    try:
        info = w.console_info(int(pid))
        d["title"], d["original"] = info["title"], info["original"]
    except OSError as e:
        d["title"] = d["original"] = None
        d["attach_error"] = str(e)
    d["tabs"], d["pick"] = [], ""
    if d["kind"] == "wt" and d["consoles"] > 1:
        uia = _uia()
        if uia is None:
            d["pick"] = f"uia unavailable: {_uia_error}"
        else:
            tabs = uia.tabs(d["host"])
            d["tabs"] = [(t.name, t.selected()) for t in tabs]
            if len(tabs) <= 1:
                d["pick"] = "single"
            elif d["title"] is not None:
                tab, art = _match(tabs, d["title"], None)
                if tab is None:
                    d["pick"] = "marker (or original %r)" % d["original"]
                else:
                    d["pick"] = f"{art} -> tab[{tabs.index(tab)}]"
    elif d["host"]:
        d["pick"] = "single" if d["kind"] in ("wt", "conhost") else "window only"
    return d


# -- Kommandozeile -------------------------------------------------------------

def _own_console() -> bool:
    buf = (W.DWORD * 1)()
    return bool(ctypes.WinDLL("kernel32").GetConsoleProcessList(buf, 1))


def _cli_list() -> int:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import sessions_live
    snap = sessions_live.LiveSessions().snapshot() or []
    print(f"{len(snap)} live sessions")
    for s in snap:
        try:
            d = describe(s["pid"], s.get("cwd"))
        except Exception as e:
            print(f"pid {s['pid']} {s['name']!r}: ERROR {type(e).__name__}: {e}")
            continue
        print(f"pid {s['pid']} {s['name']!r} [{s['state']}] folder={s.get('folder')!r}")
        print(f"   console {d['console']:#x} via {d['via'] or '-'}; host {d['host']:#x} "
              f"{d['kind'] or '-'} {d['host_class']!r} {d['host_title']!r}; "
              f"consoles in host: {d['consoles']}")
        print(f"   console title {d['title']!r} original {d['original']!r}"
              + (f" attach error: {d['attach_error']}" if d.get("attach_error") else ""))
        if d["tabs"]:
            print("   tabs: " + ", ".join(f"[{i}] {n!r}{' *' if sel else ''}"
                                         for i, (n, sel) in enumerate(d["tabs"])))
        print(f"   would pick: {d['pick'] or '-'}")
    return 0


def _cli(argv) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: termfocus.py --list | <pid> [cwd]")
        return 2
    if argv[0] == "--list":
        return _cli_list()
    t0 = time.perf_counter()
    res = focus_session(int(argv[0]), argv[1] if len(argv) > 1 else None)
    print(res, f"(wall {int((time.perf_counter() - t0) * 1000)} ms)")
    return 0 if res["ok"] else 1


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--worker":
        # Der Kindprozess hat eine eigene, fensterlose Konsole (der venv-
        # Starter gibt sie dem echten python.exe weiter): loslassen, die
        # Ausgabe geht ueber die Pipe.
        if _own_console():
            ctypes.WinDLL("kernel32").FreeConsole()
        return _cli(argv[1:])
    if _own_console():
        # AttachConsole geht nur ohne eigene Konsole: dieselbe Arbeit in einem
        # Kindprozess (CREATE_NO_WINDOW, kein aufblitzendes Fenster), der seine
        # Konsole loslaesst; Ausgabe durchreichen.
        import subprocess
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        r = subprocess.run([sys.executable, os.path.abspath(__file__), "--worker", *argv],
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, env=env, creationflags=0x08000000)
        out = r.stdout.decode("utf-8", "replace")
        sys.stdout.buffer.write(out.encode(sys.stdout.encoding or "utf-8", "replace"))
        sys.stdout.flush()
        return r.returncode
    return _cli(argv)


if __name__ == "__main__":
    sys.exit(main())
