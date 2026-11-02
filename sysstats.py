"""PC-Systemwerte fuer den "System"-Bildschirm des Clawdmeter.

SystemStats.sample() liefert ein Dict, das der ClawdmeterLink als
{"k":"sy", ...} alle 2 Sekunden schickt (ab Protokoll 2):

    c   CPU gesamt in %             cc  je logischem Kern in % (hoechstens 24)
    m   RAM belegt in MB            mt  RAM gesamt in MB
    g   GPU-Last in %               gm / gt  VRAM belegt / gesamt in MB
    dn  Systemlaufwerk ("C:")       du / dt  davon belegt / gesamt in GB
    dr  / dw  Lesen / Schreiben aller Datentraeger in KB/s
    nd  / nu  Netzwerk empfangen / gesendet in KB/s

Alle Zahlen sind ganze Zahlen; MB/GB/KB sind Binaer-Einheiten (1024er), wie
im Task-Manager. g/gm/gt gibt es nur mit einer NVIDIA-Karte (pynvml aus dem
Paket nvidia-ml-py, nur die erste Karte), sonst fehlen sie. Raten entstehen
aus der Differenz zum vorigen Aufruf ueber die tatsaechlich vergangene Zeit;
der erste Aufruf liefert dafuer 0.
"""

from __future__ import annotations

import os
import sys
import time

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


def _open_gpu():
    """Erste NVIDIA-Karte ueber NVML: (pynvml, handle) oder None. Ohne Treiber
    oder Paket schlaegt nvmlInit fehl - dann eben keine GPU-Werte."""
    try:
        import pynvml
        pynvml.nvmlInit()
        if int(pynvml.nvmlDeviceGetCount()) < 1:
            return None
        return pynvml, pynvml.nvmlDeviceGetHandleByIndex(0)
    except Exception:
        return None


class SystemStats:
    """Sammelt die Werte und merkt sich die Zaehler des vorigen Aufrufs."""

    def __init__(self, clock=time.monotonic):
        if psutil is None:
            raise RuntimeError("psutil not available")
        self._clock = clock
        self._drive_name, self._drive_path = system_drive()
        self._gpu = _open_gpu()
        self._prev = None            # (Zeit, Disk-Zaehler, Netz-Zaehler)
        # Erster Aufruf setzt nur den Bezugspunkt (liefert sonst 0.0 je Kern).
        psutil.cpu_percent(interval=None, percpu=True)

    @property
    def has_gpu(self) -> bool:
        return self._gpu is not None

    def _gpu_values(self) -> dict:
        if self._gpu is None:
            return {}
        nv, h = self._gpu
        try:
            util = nv.nvmlDeviceGetUtilizationRates(h)
            mem = nv.nvmlDeviceGetMemoryInfo(h)
            return {"g": _pct(util.gpu), "gm": int(mem.used // _MB),
                    "gt": int(mem.total // _MB)}
        except Exception:
            return {}            # Karte kurz nicht lesbar: diesmal ohne GPU

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
        out.update(self._gpu_values())
        out.update({"dn": self._drive_name,
                    "du": int(du.used // _GB), "dt": int(du.total // _GB),
                    "dr": rates[0], "dw": rates[1],
                    "nd": rates[2], "nu": rates[3]})
        return out
