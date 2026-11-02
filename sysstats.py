"""PC-Systemwerte fuer den "System"-Bildschirm des Clawdmeter.

SystemStats.sample() liefert ein Dict, das der ClawdmeterLink als
{"k":"sy", ...} alle 2 Sekunden schickt (ab Protokoll 2):

    c   CPU gesamt in %             cc  je logischem Kern in % (hoechstens 24)
    m   RAM belegt in MB            mt  RAM gesamt in MB
    g   GPU-Last in %               gm / gt  VRAM belegt / gesamt in MB
    gn  GPU-Hersteller kurz ("NVIDIA", "AMD", "Intel", sonst "GPU")
    dn  Systemlaufwerk ("C:")       du / dt  davon belegt / gesamt in GB
    dr  / dw  Lesen / Schreiben aller Datentraeger in KB/s
    nd  / nu  Netzwerk empfangen / gesendet in KB/s

Alle Zahlen sind ganze Zahlen; MB/GB/KB sind Binaer-Einheiten (1024er), wie
im Task-Manager. Raten entstehen aus der Differenz zum vorigen Aufruf ueber
die tatsaechlich vergangene Zeit; der erste Aufruf liefert dafuer 0.

GPU (GpuStats): herstellerneutral ueber die Windows-Leistungsindikatoren
(PDH, wie der Task-Manager) und die Adapterliste aus DXGI, beides per ctypes.
Gezeigt wird der Adapter mit dem groessten eigenen Grafikspeicher. Bei einer
NVIDIA-Karte nimmt es NVML (pynvml), wenn es da ist - das ist genauer. Die
Einstellung "clawdmeter_gpu" waehlt "any" (jede GPU), "nvidia" (nur NVIDIA)
oder "off" (nie GPU-Werte, dann wird auch nichts geoeffnet). Geht etwas
schief, fehlen g/gm/gt/gn einfach; gt fehlt auch, wenn die Groesse unbekannt
ist.
"""

from __future__ import annotations

import ctypes
import os
import re
import sys
import time
import uuid
from typing import NamedTuple

try:
    import psutil
except Exception:          # pragma: no cover - fehlt nur in kaputten Builds
    psutil = None

CORES_MAX = 24
_MB = 1024 * 1024
_GB = 1024 * 1024 * 1024


def _pct(x) -> int:
    """Prozentwert als ganze Zahl 0-100."""
    try:
        return max(0, min(100, int(float(x) + 0.5)))
    except (TypeError, ValueError):
        return 0


def downsample(values, n: int) -> list:
    """Werte auf hoechstens n Eintraege bringen: benachbarte Gruppen werden
    gemittelt (32 -> 24 heisst abwechselnd Paare und Einzelwerte). Ergebnis
    als ganze Zahlen."""
    values = list(values)
    if n <= 0:
        return []
    if len(values) <= n:
        return [_pct(v) for v in values]
    out = []
    for i in range(n):
        a, b = i * len(values) // n, (i + 1) * len(values) // n
        grp = values[a:b]
        out.append(_pct(sum(grp) / len(grp)))
    return out


def system_drive() -> tuple:
    """(Anzeigename, Pfad) des Laufwerks, auf dem Windows liegt."""
    if sys.platform == "win32":
        d = (os.environ.get("SystemDrive") or "C:").rstrip("\\/")
        return d, d + "\\"
    return "/", "/"


# --------------------------------------------------------------------------- #
#  GPU
# --------------------------------------------------------------------------- #
GPU_MODES = ("any", "nvidia", "off")
VENDOR_NVIDIA = 0x10DE
VENDOR_MICROSOFT = 0x1414        # Basic Render Driver, Remote Display usw.
_VENDORS = {0x10DE: "NVIDIA", 0x1002: "AMD", 0x1022: "AMD", 0x8086: "Intel"}


def vendor_short(vendor_id) -> str:
    """Kurzname des Herstellers fuer "gn" (ASCII, hoechstens 8 Zeichen)."""
    return _VENDORS.get(vendor_id, "GPU")


def gpu_mode(value) -> str:
    """Einstellungswert auf "any" | "nvidia" | "off" bringen (Unbekanntes
    zaehlt als "any", der Standard)."""
    v = str(value or "").strip().lower()
    return v if v in GPU_MODES else "any"


class Adapter(NamedTuple):
    name: str
    vendor: int
    dedicated: int               # eigener Grafikspeicher in Byte
    luid: tuple                  # (HighPart, LowPart) wie in den PDH-Namen
    software: bool = False


def pick_adapter(adapters, mode="any"):
    """Der Adapter fuer das Geraet: keine Software-Adapter, bei "nvidia" nur
    NVIDIA, dann der mit dem groessten eigenen Speicher (bei Gleichstand der
    erste). None, wenn keiner passt."""
    hw = [a for a in adapters
          if not a.software and a.vendor != VENDOR_MICROSOFT]
    if mode == "nvidia":
        hw = [a for a in hw if a.vendor == VENDOR_NVIDIA]
    return max(hw, key=lambda a: a.dedicated, default=None)


# Instanznamen der PDH-Zaehler, z. B.
#   GPU Engine:          pid_1234_luid_0x00000000_0x0000D1A4_phys_0_eng_0_engtype_3D
#   GPU Adapter Memory:  luid_0x00000000_0x0000D1A4_phys_0
_LUID_RE = re.compile(r"luid_0x([0-9a-fA-F]+)_0x([0-9a-fA-F]+)")
_ENGTYPE_RE = re.compile(r"_engtype_(.*)$")


def parse_luid(instance):
    """(HighPart, LowPart) aus einem Instanznamen, oder None."""
    m = _LUID_RE.search(instance or "")
    if not m:
        return None
    return int(m.group(1), 16) & 0xFFFFFFFF, int(m.group(2), 16) & 0xFFFFFFFF


def gpu_load(items) -> dict:
    """{luid: Last in %} aus (Instanz, Wert)-Paaren von "GPU Engine": je
    Adapter die Prozesse je Engine-Typ addieren, dann der groesste Typ - so
    rechnet der Task-Manager seine GPU-Prozent. Auf 0-100 begrenzt."""
    sums = {}
    for name, value in items:
        luid = parse_luid(name)
        if luid is None:
            continue
        m = _ENGTYPE_RE.search(name)
        eng = m.group(1) if m else ""
        per = sums.setdefault(luid, {})
        per[eng] = per.get(eng, 0.0) + max(0.0, float(value))
    return {luid: max(0.0, min(100.0, max(per.values())))
            for luid, per in sums.items() if per}


def gpu_memory(items) -> dict:
    """{luid: belegter eigener Speicher in Byte} aus "GPU Adapter Memory"
    (mehrere phys_N eines Adapters zusammengezaehlt)."""
    out = {}
    for name, value in items:
        luid = parse_luid(name)
        if luid is not None:
            out[luid] = out.get(luid, 0) + max(0, int(value))
    return out


# ---- DXGI: Adapterliste ------------------------------------------------------
class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", ctypes.c_ulong), ("HighPart", ctypes.c_long)]


class _DXGI_ADAPTER_DESC1(ctypes.Structure):
    _fields_ = [("Description", ctypes.c_wchar * 128),
                ("VendorId", ctypes.c_uint), ("DeviceId", ctypes.c_uint),
                ("SubSysId", ctypes.c_uint), ("Revision", ctypes.c_uint),
                ("DedicatedVideoMemory", ctypes.c_size_t),
                ("DedicatedSystemMemory", ctypes.c_size_t),
                ("SharedSystemMemory", ctypes.c_size_t),
                ("AdapterLuid", _LUID), ("Flags", ctypes.c_uint)]


_IID_IDXGIFactory1 = "770aae78-f26f-4dba-a829-253c83d1b387"
_DXGI_ERROR_NOT_FOUND = 0x887A0002
_DXGI_ADAPTER_FLAG_SOFTWARE = 2
# Plaetze in den vtables (IUnknown 0-2, IDXGIObject 3-6, dann die Klasse)
_VT_RELEASE = 2
_VT_FACTORY1_ENUMADAPTERS1 = 12
_VT_ADAPTER1_GETDESC1 = 10


def _vtbl(obj):
    return ctypes.cast(ctypes.c_void_p(obj),
                       ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents


def _com(obj, index, argtypes, *args) -> int:
    """Methode Nr. index der COM-Schnittstelle obj aufrufen (HRESULT)."""
    proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)
    return proto(_vtbl(obj)[index])(obj, *args)


def _release(obj):
    if obj:
        try:
            ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(
                _vtbl(obj)[_VT_RELEASE])(obj)
        except Exception:
            pass


def dxgi_adapters() -> list:
    """Alle Grafikadapter laut DXGI (Software-Adapter mit Kennzeichen).
    Wirft OSError, wenn DXGI nicht geht."""
    if sys.platform != "win32":
        raise OSError("DXGI needs Windows")
    create = ctypes.WinDLL("dxgi").CreateDXGIFactory1
    create.restype = ctypes.c_long
    create.argtypes = [ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
    iid = _GUID.from_buffer_copy(uuid.UUID(_IID_IDXGIFactory1).bytes_le)
    factory = ctypes.c_void_p()
    hr = create(ctypes.byref(iid), ctypes.byref(factory))
    if hr < 0 or not factory.value:
        raise OSError(f"CreateDXGIFactory1 failed: 0x{hr & 0xFFFFFFFF:08X}")
    out = []
    try:
        for i in range(16):
            adapter = ctypes.c_void_p()
            hr = _com(factory.value, _VT_FACTORY1_ENUMADAPTERS1,
                      (ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)),
                      i, ctypes.byref(adapter))
            if (hr & 0xFFFFFFFF) == _DXGI_ERROR_NOT_FOUND:
                break
            if hr < 0 or not adapter.value:
                raise OSError(f"EnumAdapters1 failed: 0x{hr & 0xFFFFFFFF:08X}")
            try:
                d = _DXGI_ADAPTER_DESC1()
                hr = _com(adapter.value, _VT_ADAPTER1_GETDESC1,
                          (ctypes.POINTER(_DXGI_ADAPTER_DESC1),), ctypes.byref(d))
                if hr < 0:
                    continue
                out.append(Adapter(
                    name=d.Description, vendor=int(d.VendorId),
                    dedicated=int(d.DedicatedVideoMemory),
                    luid=(d.AdapterLuid.HighPart & 0xFFFFFFFF,
                          d.AdapterLuid.LowPart & 0xFFFFFFFF),
                    software=bool(d.Flags & _DXGI_ADAPTER_FLAG_SOFTWARE)))
            finally:
                _release(adapter.value)
    finally:
        _release(factory.value)
    return out


# ---- PDH: Leistungsindikatoren -----------------------------------------------
class _FMT_VALUE(ctypes.Union):
    _fields_ = [("longValue", ctypes.c_long), ("doubleValue", ctypes.c_double),
                ("largeValue", ctypes.c_longlong), ("ptr", ctypes.c_void_p)]


class _FMT_COUNTERVALUE(ctypes.Structure):
    _fields_ = [("CStatus", ctypes.c_ulong), ("u", _FMT_VALUE)]


class _FMT_ITEM_W(ctypes.Structure):
    _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", _FMT_COUNTERVALUE)]


_PDH_FMT_DOUBLE = 0x00000200
_PDH_FMT_LARGE = 0x00000400
_PDH_FMT_NOCAP100 = 0x00008000
_PDH_MORE_DATA = 0x800007D2
_PDH_CSTATUS_OK = (0, 1)         # VALID_DATA, NEW_DATA
_PDH_SEVERITY_ERROR = 0xC0000000

ENGINE_COUNTER = r"\GPU Engine(*)\Utilization Percentage"
MEMORY_COUNTER = r"\GPU Adapter Memory(*)\Dedicated Usage"


class PdhGpu:
    """Eine offene PDH-Abfrage mit den beiden GPU-Zaehlern. Bleibt zwischen
    den Aufrufen offen: die Last ist eine Rate und braucht zwei Messungen,
    der erste read() liefert deshalb noch keine Engine-Werte."""

    def __init__(self):
        if sys.platform != "win32":
            raise OSError("PDH needs Windows")
        pdh = ctypes.WinDLL("pdh")
        H = ctypes.c_void_p
        pdh.PdhOpenQueryW.argtypes = [ctypes.c_wchar_p, ctypes.c_size_t,
                                      ctypes.POINTER(H)]
        pdh.PdhAddEnglishCounterW.argtypes = [H, ctypes.c_wchar_p,
                                              ctypes.c_size_t, ctypes.POINTER(H)]
        pdh.PdhCollectQueryData.argtypes = [H]
        pdh.PdhGetFormattedCounterArrayW.argtypes = [
            H, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p]
        pdh.PdhCloseQuery.argtypes = [H]
        for f in (pdh.PdhOpenQueryW, pdh.PdhAddEnglishCounterW,
                  pdh.PdhCollectQueryData, pdh.PdhGetFormattedCounterArrayW,
                  pdh.PdhCloseQuery):
            f.restype = ctypes.c_ulong
        self._pdh = pdh
        self._query = H()
        st = pdh.PdhOpenQueryW(None, 0, ctypes.byref(self._query))
        if st:
            raise OSError(f"PdhOpenQuery failed: 0x{st:08X}")
        try:
            self._engine = self._add(ENGINE_COUNTER)
            self._memory = self._add(MEMORY_COUNTER)
        except Exception:
            self.close()
            raise

    def _add(self, path):
        h = ctypes.c_void_p()
        st = self._pdh.PdhAddEnglishCounterW(self._query, path, 0, ctypes.byref(h))
        if st:
            raise OSError(f"PdhAddEnglishCounter({path}) failed: 0x{st:08X}")
        return h

    def _array(self, counter, fmt) -> list:
        """[(Instanz, Wert)] eines Platzhalter-Zaehlers; [] wenn er (noch)
        keine gueltigen Werte hat - etwa die Rate nach der ersten Messung."""
        for _ in range(3):
            size, count = ctypes.c_ulong(0), ctypes.c_ulong(0)
            st = self._pdh.PdhGetFormattedCounterArrayW(
                counter, fmt, ctypes.byref(size), ctypes.byref(count), None)
            if st != _PDH_MORE_DATA:
                return []
            buf = ctypes.create_string_buffer(size.value)
            st = self._pdh.PdhGetFormattedCounterArrayW(
                counter, fmt, ctypes.byref(size), ctypes.byref(count), buf)
            if st == _PDH_MORE_DATA:
                continue
            if st:
                return []
            items = ctypes.cast(buf, ctypes.POINTER(_FMT_ITEM_W))
            out = []
            for i in range(count.value):
                it = items[i]
                if it.FmtValue.CStatus not in _PDH_CSTATUS_OK:
                    continue
                v = (it.FmtValue.u.doubleValue if fmt & _PDH_FMT_DOUBLE
                     else it.FmtValue.u.largeValue)
                out.append((it.szName or "", v))
            return out
        return []

    def read(self):
        """Neue Messung: ([(Instanz, Last %)], [(Instanz, Byte)])."""
        st = self._pdh.PdhCollectQueryData(self._query)
        # PDH_NO_DATA (keine Instanzen) ist eine Warnung, kein Fehler.
        if st and (st & _PDH_SEVERITY_ERROR) == _PDH_SEVERITY_ERROR:
            raise OSError(f"PdhCollectQueryData failed: 0x{st:08X}")
        return (self._array(self._engine, _PDH_FMT_DOUBLE | _PDH_FMT_NOCAP100),
                self._array(self._memory, _PDH_FMT_LARGE))

    def close(self):
        q, self._query = self._query, ctypes.c_void_p()
        if q:
            try:
                self._pdh.PdhCloseQuery(q)
            except Exception:
                pass


# ---- NVML (genauer bei NVIDIA) -----------------------------------------------
def _open_nvml(name=None):
    """NVIDIA-Karte ueber NVML: (pynvml, handle) oder None. Mit name die Karte
    gleichen Namens (DXGI-Beschreibung); ohne Treffer bzw. ohne name nur dann
    Karte 0, wenn es genau eine gibt. Ohne Treiber oder Paket schlaegt
    nvmlInit fehl - dann eben nicht."""
    try:
        import pynvml
        pynvml.nvmlInit()
    except Exception:
        return None
    try:
        count = int(pynvml.nvmlDeviceGetCount())
        want = (name or "").strip().casefold()
        if want:
            for i in range(count):
                h = pynvml.nvmlDeviceGetHandleByIndex(i)
                n = pynvml.nvmlDeviceGetName(h)
                if isinstance(n, bytes):
                    n = n.decode("utf-8", "replace")
                if str(n).strip().casefold() == want:
                    return pynvml, h
        if count == 1:
            return pynvml, pynvml.nvmlDeviceGetHandleByIndex(0)
    except Exception:
        pass
    try:
        pynvml.nvmlShutdown()
    except Exception:
        pass
    return None


class GpuStats:
    """GPU-Werte g/gm/gt/gn fuer sample(). Die Quelle (NVML oder PDH) wird
    erst beim ersten Aufruf geoeffnet und bleibt offen; wechselt die
    Einstellung, wird sie geschlossen und passend neu geoeffnet. Wirft nie:
    bei jedem Fehler kommt {} zurueck."""

    RETRY = 60.0                 # Sekunden bis zum naechsten Oeffnungsversuch
    MAX_MISSES = 3               # so oft Adapter nicht in PDH -> neu aufzaehlen

    def __init__(self, mode="any", clock=time.monotonic, adapters=None,
                 pdh=None, nvml=None):
        # mode: fester Wert oder Funktion, die die aktuelle Einstellung liefert
        self._mode_src = mode
        self._clock = clock
        # Ersatzteile fuer Tests; None = die echten (zur Laufzeit nachgeschlagen)
        self._adapters_fn = adapters
        self._pdh_fn = pdh
        self._nvml_fn = nvml
        self._mode = None
        self._src = None         # ("nvml", (pynvml, handle)) | ("pdh", PdhGpu)
        self._adapter = None     # gewaehlter Adapter (bei PDH ohne DXGI None)
        self._pending = None     # erste PDH-Messung aus dem Oeffnen
        self._next_try = 0.0
        self._misses = 0

    @property
    def is_open(self) -> bool:
        return self._src is not None

    @property
    def adapter(self):
        return self._adapter

    def mode(self) -> str:
        src = self._mode_src
        try:
            return gpu_mode(src() if callable(src) else src)
        except Exception:
            return "any"

    def close(self):
        src, self._src, self._adapter, self._misses = self._src, None, None, 0
        self._pending = None
        if src is None:
            return
        kind, obj = src
        try:
            if kind == "pdh":
                obj.close()
            else:
                obj[0].nvmlShutdown()
        except Exception:
            pass

    def _open(self, mode):
        list_adapters = self._adapters_fn or dxgi_adapters
        open_nvml = self._nvml_fn or _open_nvml
        make_pdh = self._pdh_fn or PdhGpu
        try:
            adapters = list(list_adapters())
        except Exception:
            adapters = None
        if adapters is not None:
            a = pick_adapter(adapters, mode)
            if a is None:
                return                       # keine (passende) GPU
            if a.vendor == VENDOR_NVIDIA:
                nv = open_nvml(a.name)
                if nv is not None:
                    self._src, self._adapter = ("nvml", nv), a
                    return
            pdh, first = self._open_pdh(make_pdh)
            try:
                # DXGI kennt manchmal Adapter, fuer die es keine Zaehler gibt
                # (gesehen: dieselbe Radeon zweimal, eine davon ohne PDH-Daten).
                # Dann unter denen waehlen, die PDH wirklich misst.
                present = set(gpu_load(first[0])) | set(gpu_memory(first[1]))
            except Exception:
                pdh.close()
                raise
            if present and a.luid not in present:
                b = pick_adapter([x for x in adapters if x.luid in present], mode)
                if b is not None:
                    a = b
            self._src, self._adapter, self._pending = ("pdh", pdh), a, first
            return
        # Ohne DXGI: NVML, wenn es geht (dann ist es sicher NVIDIA); sonst
        # nur bei "any" PDH allein mit dem gerade meistbeschaeftigten Adapter.
        nv = open_nvml(None)
        if nv is not None:
            self._src = ("nvml", nv)
        elif mode == "any":
            pdh, first = self._open_pdh(make_pdh)
            self._src, self._pending = ("pdh", pdh), first

    @staticmethod
    def _open_pdh(make_pdh):
        """PDH-Abfrage oeffnen und gleich einmal messen (Bezugspunkt fuer die
        Rate; die Speicherwerte sind sofort da)."""
        pdh = make_pdh()
        try:
            return pdh, pdh.read()
        except Exception:
            pdh.close()
            raise

    @staticmethod
    def _read_nvml(nv) -> dict:
        mod, h = nv
        util = mod.nvmlDeviceGetUtilizationRates(h)
        mem = mod.nvmlDeviceGetMemoryInfo(h)
        return {"g": _pct(util.gpu), "gm": int(mem.used // _MB),
                "gt": int(mem.total // _MB), "gn": "NVIDIA"}

    def _read_pdh(self, pdh) -> dict:
        # Direkt nach dem Oeffnen die Messung von dort nehmen: eine zweite
        # sofort danach ergaebe eine Rate ueber wenige Millisekunden.
        first, self._pending = self._pending, None
        engines, memory = first if first is not None else pdh.read()
        load, used = gpu_load(engines), gpu_memory(memory)
        a = self._adapter
        if a is not None:
            luid = a.luid
            if luid not in load and luid not in used:
                # Adapter weg (Treiber neu, eGPU ab)? Nach ein paar Runden neu
                # aufzaehlen, statt fuer immer nichts zu zeigen.
                self._misses += 1
                if self._misses >= self.MAX_MISSES:
                    self.close()
                    self._next_try = self._clock() + 5.0
                return {}
        else:
            luids = set(load) | set(used)
            if not luids:
                return {}
            luid = max(luids, key=lambda k: (load.get(k, 0.0), used.get(k, 0)))
        self._misses = 0
        out = {"g": _pct(load.get(luid, 0.0))}
        if luid in used:
            out["gm"] = int(used[luid] // _MB)
        if a is not None and a.dedicated > 0:
            out["gt"] = int(a.dedicated // _MB)
        out["gn"] = vendor_short(a.vendor) if a is not None else "GPU"
        return out

    def values(self) -> dict:
        try:
            mode = self.mode()
            if mode != self._mode:
                self.close()
                self._mode, self._next_try = mode, 0.0
            if mode == "off":
                return {}
            if self._src is None:
                now = self._clock()
                if now < self._next_try:
                    return {}
                self._next_try = now + self.RETRY
                try:
                    self._open(mode)
                except Exception:
                    self.close()
                if self._src is None:
                    return {}
            kind, obj = self._src
            return self._read_nvml(obj) if kind == "nvml" else self._read_pdh(obj)
        except Exception:
            return {}            # kurz nicht lesbar: diesmal ohne GPU


class SystemStats:
    """Sammelt die Werte und merkt sich die Zaehler des vorigen Aufrufs."""

    def __init__(self, clock=time.monotonic, gpu_mode="any", gpu=None):
        # gpu_mode: "any" | "nvidia" | "off" oder eine Funktion, die den
        # aktuellen Wert liefert (dann wirkt eine Aenderung sofort).
        if psutil is None:
            raise RuntimeError("psutil not available")
        self._clock = clock
        self._drive_name, self._drive_path = system_drive()
        self._gpu = gpu if gpu is not None else GpuStats(gpu_mode, clock=clock)
        self._prev = None            # (Zeit, Disk-Zaehler, Netz-Zaehler)
        # Erster Aufruf setzt nur den Bezugspunkt (liefert sonst 0.0 je Kern).
        psutil.cpu_percent(interval=None, percpu=True)

    @property
    def has_gpu(self) -> bool:
        return self._gpu.is_open

    def sample(self) -> dict:
        now = self._clock()
        cores = psutil.cpu_percent(interval=None, percpu=True) or [0.0]
        vm = psutil.virtual_memory()
        du = psutil.disk_usage(self._drive_path)
        dio = psutil.disk_io_counters()
        nio = psutil.net_io_counters()
        disk = (dio.read_bytes, dio.write_bytes) if dio else (0, 0)
        net = (nio.bytes_recv, nio.bytes_sent) if nio else (0, 0)

        rates = [0, 0, 0, 0]
        if self._prev is not None:
            t0, disk0, net0 = self._prev
            dt = now - t0
            if dt > 0:
                # Zaehler koennen zuruecksetzen (Adapter neu) -> nie negativ.
                rates = [max(0, int((b - a) / dt / 1024 + 0.5))
                         for a, b in zip(disk0 + net0, disk + net)]
        self._prev = (now, disk, net)

        out = {"c": _pct(sum(cores) / len(cores)),
               "cc": downsample(cores, CORES_MAX),
               "m": int((vm.total - vm.available) // _MB),
               "mt": int(vm.total // _MB)}
        out.update(self._gpu.values())
        out.update({"dn": self._drive_name,
                    "du": int(du.used // _GB), "dt": int(du.total // _GB),
                    "dr": rates[0], "dw": rates[1],
                    "nd": rates[2], "nu": rates[3]})
        return out
