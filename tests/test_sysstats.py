"""PC system stats for the Clawdmeter "System" screen (sysstats.py)."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import clawdmeter as cm  # noqa: E402
import sysstats  # noqa: E402
from sysstats import Adapter  # noqa: E402

MB, GB = 1024 * 1024, 1024 ** 3


class FakePsutil:
    """Just the psutil calls sysstats uses; tests move the counters."""

    def __init__(self, cores):
        self.cores = list(cores)
        self.disk = NS(read_bytes=0, write_bytes=0)
        self.net = NS(bytes_recv=0, bytes_sent=0)
        self.paths = []

    def cpu_percent(self, interval=None, percpu=False):
        assert interval is None and percpu   # never blocks
        return list(self.cores)

    def virtual_memory(self):
        return NS(total=32 * GB, available=20 * GB, used=12 * GB)

    def disk_usage(self, path):
        self.paths.append(path)
        return NS(total=int(931.5 * GB), used=400 * GB + 123)

    def disk_io_counters(self):
        return self.disk

    def net_io_counters(self):
        return self.net


class FakeNvml:
    def __init__(self, fail_init=False, count=1, names=("NVIDIA GeForce RTX 4090",)):
        self.fail_init, self.count, self.names = fail_init, count, list(names)
        self.shutdown = 0

    def nvmlInit(self):
        if self.fail_init:
            raise RuntimeError("NVML Shared Library Not Found")

    def nvmlShutdown(self):
        self.shutdown += 1

    def nvmlDeviceGetCount(self):
        return self.count

    def nvmlDeviceGetHandleByIndex(self, i):
        assert 0 <= i < self.count
        return f"gpu{i}"

    def nvmlDeviceGetName(self, h):
        return self.names[int(h[3:])].encode()        # older pynvml returns bytes

    def nvmlDeviceGetUtilizationRates(self, h):
        return NS(gpu=37 + int(h[3:]), memory=5)

    def nvmlDeviceGetMemoryInfo(self, h):
        return NS(total=24 * GB, used=int(3.5 * GB), free=0)


# ---- GPU fakes ----------------------------------------------------------------
AMD = Adapter("AMD Radeon(TM) 8060S Graphics", 0x1002, 32590 * MB, (0, 0x1A5C9))
AMD_IGPU = Adapter("AMD Radeon(TM) Graphics", 0x1002, 512 * MB, (0, 0x5000))
NV = Adapter("NVIDIA GeForce RTX 4090", 0x10DE, 24 * GB, (0, 0x2000))
INTEL = Adapter("Intel(R) UHD Graphics", 0x8086, 128 * MB, (0, 0x3000))
BASIC = Adapter("Microsoft Basic Render Driver", 0x1414, 0, (0, 0x1CBC5), software=True)


def eng(luid, pid, engtype, value, idx=0):
    return (f"pid_{pid}_luid_0x{luid[0]:08X}_0x{luid[1]:08X}"
            f"_phys_0_eng_{idx}_engtype_{engtype}", value)


def mem(luid, value, phys=0):
    return (f"luid_0x{luid[0]:08X}_0x{luid[1]:08X}_phys_{phys}", value)


class FakePdh:
    """Plays back read() results (a list; the last one repeats). An Exception
    in the list is raised instead."""

    def __init__(self, reads):
        self.reads = list(reads)
        self.calls = 0
        self.closed = False

    def read(self):
        r = self.reads[min(self.calls, len(self.reads) - 1)]
        self.calls += 1
        if isinstance(r, Exception):
            raise r
        return r

    def close(self):
        self.closed = True


class Factory:
    """Counts how often a layer is opened."""

    def __init__(self, result):
        self.result, self.calls = result, 0

    def __call__(self, *a):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def no_dxgi():
    raise OSError("no DXGI")


def no_pdh():
    raise OSError("no PDH")


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def make(monkeypatch, cores=(10, 20, 30, 40), nvml=None, adapters=no_dxgi,
         pdh=no_pdh, mode="any"):
    ps = FakePsutil(cores)
    monkeypatch.setattr(sysstats, "psutil", ps)
    if nvml is None:
        monkeypatch.setitem(sys.modules, "pynvml", None)   # import fails
    else:
        monkeypatch.setitem(sys.modules, "pynvml", nvml)
    monkeypatch.setenv("SystemDrive", "C:")
    clock = Clock()
    gpu = sysstats.GpuStats(mode, clock=clock, adapters=adapters, pdh=pdh)
    return sysstats.SystemStats(clock=clock, gpu=gpu), ps, clock


def gpu(mode="any", adapters=None, pdh=None, nvml=None, clock=None):
    """GpuStats with every layer faked; the real ones must never run here."""
    return sysstats.GpuStats(mode, clock=clock or Clock(),
                             adapters=adapters or Factory([]),
                             pdh=pdh or Factory(OSError("no PDH")),
                             nvml=nvml or Factory(None))


# ---- general fields -------------------------------------------------------------
def test_sample_fields_and_units(monkeypatch):
    st, ps, _ = make(monkeypatch, nvml=FakeNvml(), adapters=lambda: [NV])
    s = st.sample()
    assert list(s) == ["c", "cc", "m", "mt", "g", "gm", "gt", "gn",
                       "dn", "du", "dt", "dr", "dw", "nd", "nu"]
    assert all(isinstance(v, int) for k, v in s.items()
               if k not in ("cc", "dn", "gn"))
    assert s["c"] == 25 and s["cc"] == [10, 20, 30, 40]
    assert s["m"] == 12 * 1024 and s["mt"] == 32 * 1024
    assert s["g"] == 37 and s["gm"] == 3584 and s["gt"] == 24576
    assert s["gn"] == "NVIDIA"
    if sys.platform == "win32":
        assert s["dn"] == "C:" and ps.paths[-1] == "C:\\"
    assert s["du"] == 400 and s["dt"] == 931
    # First call: no previous counters, so rates are zero.
    assert [s[k] for k in ("dr", "dw", "nd", "nu")] == [0, 0, 0, 0]


def test_gpu_absent_omits_keys(monkeypatch):
    for nvml in (None, FakeNvml(fail_init=True), FakeNvml(count=0)):
        st, _, _ = make(monkeypatch, nvml=nvml)          # no DXGI, no PDH
        s = st.sample()
        assert not st.has_gpu
        assert not {"g", "gm", "gt", "gn"} & set(s)
        assert {"c", "cc", "m", "mt", "dn", "du", "dt", "dr", "dw", "nd", "nu"} <= set(s)


def test_gpu_read_error_skips_gpu_for_that_sample(monkeypatch):
    nv = FakeNvml()
    st, _, _ = make(monkeypatch, nvml=nv, adapters=lambda: [NV])
    assert st.sample()["g"] == 37
    nv.nvmlDeviceGetMemoryInfo = lambda h: (_ for _ in ()).throw(RuntimeError("gone"))
    assert "g" not in st.sample()
    del nv.nvmlDeviceGetMemoryInfo                        # back to the class method
    assert st.sample()["g"] == 37


def test_rates_use_real_elapsed_time_and_never_go_negative(monkeypatch):
    st, ps, clock = make(monkeypatch)
    st.sample()
    clock.t += 2.5
    ps.disk = NS(read_bytes=2560 * 1024, write_bytes=1024 * 1024)   # +2.5 MB / +1 MB
    ps.net = NS(bytes_recv=5 * 1024 * 1024, bytes_sent=250 * 1024)
    s = st.sample()
    assert (s["dr"], s["dw"], s["nd"], s["nu"]) == (1024, 410, 2048, 100)
    clock.t += 2.0                                       # counters reset (adapter restart)
    ps.net = NS(bytes_recv=0, bytes_sent=0)
    s = st.sample()
    assert s["nd"] == 0 and s["nu"] == 0 and s["dr"] == 0
    s = st.sample()                                      # no time passed
    assert s["dr"] == 0


def test_missing_io_counters_mean_zero(monkeypatch):
    st, ps, clock = make(monkeypatch)
    ps.disk = ps.net = None
    st.sample()
    clock.t += 2
    s = st.sample()
    assert (s["dr"], s["dw"], s["nd"], s["nu"]) == (0, 0, 0, 0)


def test_cores_are_averaged_down_to_24(monkeypatch):
    cores = [float(i % 2) * 100 for i in range(32)]      # 0,100,0,100,...
    st, _, _ = make(monkeypatch, cores=cores)
    s = st.sample()
    assert len(s["cc"]) == 24 and s["c"] == 50
    assert all(0 <= v <= 100 and isinstance(v, int) for v in s["cc"])
    assert sysstats.downsample(range(48), 24) == [int(2 * i + 0.5 + 0.5) for i in range(24)]
    assert sysstats.downsample([5, 7.6], 24) == [5, 8]
    assert sysstats.downsample([], 24) == []


def test_downsample_keeps_groups_adjacent():
    vals = [0] * 16 + [100] * 16                          # first half idle, second busy
    out = sysstats.downsample(vals, 24)
    assert out[:12] == [0] * 12 and out[12:] == [100] * 12


def test_without_psutil_the_collector_refuses(monkeypatch):
    monkeypatch.setattr(sysstats, "psutil", None)
    with pytest.raises(RuntimeError):
        sysstats.SystemStats()


# ---- GPU: pure helpers ------------------------------------------------------------
def test_vendor_short_names():
    assert sysstats.vendor_short(0x10DE) == "NVIDIA"
    assert sysstats.vendor_short(0x1002) == "AMD"
    assert sysstats.vendor_short(0x1022) == "AMD"
    assert sysstats.vendor_short(0x8086) == "Intel"
    assert sysstats.vendor_short(0x5143) == "GPU"           # Qualcomm etc.
    for v in (0x10DE, 0x1002, 0x8086, 0x1234):
        n = sysstats.vendor_short(v)
        assert n.isascii() and 0 < len(n) <= 8


def test_gpu_mode_normalises():
    assert [sysstats.gpu_mode(v) for v in ("any", "NVIDIA", " off ", "", None, "x")] \
        == ["any", "nvidia", "off", "any", "any", "any"]


def test_pick_adapter_largest_dedicated_memory_and_no_software():
    assert sysstats.pick_adapter([INTEL, NV, AMD_IGPU]) == NV
    assert sysstats.pick_adapter([AMD_IGPU, INTEL]) == AMD_IGPU
    # Software adapters never win, flagged or only by Microsoft's vendor id.
    big_basic = BASIC._replace(dedicated=64 * GB)
    remote = Adapter("Microsoft Remote Display Adapter", 0x1414, 64 * GB, (0, 9))
    assert sysstats.pick_adapter([big_basic, remote, INTEL]) == INTEL
    assert sysstats.pick_adapter([BASIC]) is None
    assert sysstats.pick_adapter([]) is None
    # Tie: the first one (DXGI lists the primary adapter first).
    twin = AMD._replace(luid=(0, 0x294EF))
    assert sysstats.pick_adapter([AMD, twin]) == AMD


def test_pick_adapter_nvidia_only():
    assert sysstats.pick_adapter([AMD, INTEL, NV], "nvidia") == NV
    assert sysstats.pick_adapter([AMD, INTEL, BASIC], "nvidia") is None
    small_nv = NV._replace(dedicated=2 * GB, luid=(0, 0x2001))
    assert sysstats.pick_adapter([AMD, small_nv], "nvidia") == small_nv


def test_parse_luid_high_then_low():
    assert sysstats.parse_luid(
        "pid_1234_luid_0x00000000_0x0000D1A4_phys_0_eng_0_engtype_3D") == (0, 0xD1A4)
    assert sysstats.parse_luid("luid_0x00000001_0xFFFFFFFE_phys_0") == (1, 0xFFFFFFFE)
    assert sysstats.parse_luid("_Total") is None
    assert sysstats.parse_luid("") is None


def test_gpu_load_sums_per_engtype_then_max():
    A, B = (0, 0xA), (0, 0xB)
    items = [eng(A, 1, "3D", 30.0), eng(A, 2, "3D", 25.5),       # 3D = 55.5
             eng(A, 1, "Copy", 40.0),                            # Copy = 40
             eng(A, 3, "VideoDecode", 10.0, idx=4),
             eng(B, 1, "Compute 0", 80.0), eng(B, 2, "Compute 0", 70.0),  # 150 -> 100
             eng(B, 2, "3D", -5.0),                              # negative ignored
             ("_Total", 99.0)]                                   # no LUID
    load = sysstats.gpu_load(items)
    assert load == {A: 55.5, B: 100.0}
    assert sysstats.gpu_load([]) == {}


def test_gpu_memory_sums_phys_per_luid():
    A, B = (0, 0xA), (0, 0xB)
    used = sysstats.gpu_memory([mem(A, 3 * GB), mem(A, 1 * GB, phys=1), mem(B, 0)])
    assert used == {A: 4 * GB, B: 0}


# ---- GPU: source selection --------------------------------------------------------
def amd_reads(load_3d=42.0):
    """Two adapters in PDH (the AMD and the Basic Render Driver)."""
    warm = ([], [mem(AMD.luid, 4800 * MB), mem(BASIC.luid, 0)])     # rates not ready yet
    live = ([eng(AMD.luid, 10, "3D", load_3d), eng(AMD.luid, 11, "3D", 3.0),
             eng(AMD.luid, 10, "Copy", 1.0), eng(BASIC.luid, 4, "3D", 90.0)],
            [mem(AMD.luid, 4900 * MB), mem(BASIC.luid, 0)])
    return [warm, live]


def test_pdh_path_amd_matches_luid_and_uses_dxgi_total():
    fp = FakePdh(amd_reads())
    pdh = Factory(fp)
    g = gpu(adapters=Factory([AMD, BASIC]), pdh=pdh)
    first = g.values()
    # First sample comes from the warm-up read at opening: load 0, VRAM known.
    assert first == {"g": 0, "gm": 4800, "gt": 32590, "gn": "AMD"}
    assert pdh.calls == 1 and fp.calls == 1
    second = g.values()
    # Only the AMD LUID counts (the Basic Render Driver's 90 % is ignored).
    assert second == {"g": 45, "gm": 4900, "gt": 32590, "gn": "AMD"}
    assert list(second) == ["g", "gm", "gt", "gn"]
    assert pdh.calls == 1 and fp.calls == 2              # query stays open


def test_duplicate_dxgi_adapter_without_pdh_counters_is_skipped():
    twin = AMD._replace(luid=(0, 0x294EF), dedicated=AMD.dedicated + 1)
    g = gpu(adapters=Factory([twin, AMD, BASIC]), pdh=Factory(FakePdh(amd_reads())))
    g.values()
    assert g.adapter == AMD
    assert g.values()["g"] == 45


def test_nvidia_with_nvml_prefers_nvml_and_matches_by_name(monkeypatch):
    nv = FakeNvml(count=2, names=("NVIDIA T400", "NVIDIA GeForce RTX 4090"))
    monkeypatch.setitem(sys.modules, "pynvml", nv)
    pdh = Factory(FakePdh(amd_reads()))
    g = sysstats.GpuStats("any", clock=Clock(), adapters=Factory([AMD_IGPU, NV]), pdh=pdh)
    v = g.values()
    assert v == {"g": 38, "gm": 3584, "gt": 24576, "gn": "NVIDIA"}   # device 1
    assert pdh.calls == 0


def test_nvidia_without_nvml_uses_pdh():
    reads = [([], [mem(NV.luid, 2 * GB)]),
             ([eng(NV.luid, 1, "3D", 12.4)], [mem(NV.luid, 2 * GB)])]
    g = gpu(adapters=Factory([AMD_IGPU, NV]), pdh=Factory(FakePdh(reads)))
    g.values()
    assert g.values() == {"g": 12, "gm": 2048, "gt": 24576, "gn": "NVIDIA"}


def test_open_nvml_ambiguous_name_with_several_cards_gives_up(monkeypatch):
    nv = FakeNvml(count=2, names=("A", "B"))
    monkeypatch.setitem(sys.modules, "pynvml", nv)
    assert sysstats._open_nvml("C") is None and nv.shutdown == 1
    assert sysstats._open_nvml("b") == (nv, "gpu1")          # case-insensitive
    one = FakeNvml(count=1, names=("Some other name",))
    monkeypatch.setitem(sys.modules, "pynvml", one)
    assert sysstats._open_nvml("NVIDIA GeForce RTX 4090") == (one, "gpu0")


def test_nvidia_only_mode():
    # With an NVIDIA card next to a bigger AMD: the NVIDIA one.
    reads = [([], [mem(NV.luid, GB), mem(AMD.luid, GB)])]
    g = gpu("nvidia", adapters=Factory([AMD, NV]), pdh=Factory(FakePdh(reads)))
    assert g.values()["gn"] == "NVIDIA" and g.adapter == NV
    # Without one: no GPU keys, and PDH is never opened.
    pdh = Factory(FakePdh(amd_reads()))
    g = gpu("nvidia", adapters=Factory([AMD, INTEL, BASIC]), pdh=pdh)
    assert g.values() == {} and g.values() == {}
    assert pdh.calls == 0 and not g.is_open


def test_nvidia_only_without_dxgi_needs_nvml(monkeypatch):
    pdh = Factory(FakePdh(amd_reads()))
    g = gpu("nvidia", adapters=Factory(OSError("no DXGI")), pdh=pdh)
    assert g.values() == {} and pdh.calls == 0
    nv = FakeNvml()
    monkeypatch.setitem(sys.modules, "pynvml", nv)
    g = sysstats.GpuStats("nvidia", clock=Clock(), adapters=Factory(OSError("x")), pdh=pdh)
    assert g.values()["gn"] == "NVIDIA" and pdh.calls == 0


def test_off_mode_opens_nothing():
    adapters, pdh, nvml = Factory([AMD]), Factory(FakePdh(amd_reads())), Factory(None)
    g = sysstats.GpuStats("off", clock=Clock(), adapters=adapters, pdh=pdh, nvml=nvml)
    for _ in range(3):
        assert g.values() == {}
    assert adapters.calls == pdh.calls == nvml.calls == 0


def test_off_mode_in_system_stats_has_no_gpu_keys(monkeypatch):
    st, _, _ = make(monkeypatch, nvml=FakeNvml(), adapters=lambda: [NV], mode="off")
    s = st.sample()
    assert not {"g", "gm", "gt", "gn"} & set(s) and not st.has_gpu


def test_mode_change_takes_effect_live():
    setting = {"v": "any"}
    fp = FakePdh(amd_reads())
    pdh = Factory(fp)
    g = gpu(lambda: setting["v"], adapters=Factory([AMD]), pdh=pdh)
    assert g.values()["gn"] == "AMD"
    setting["v"] = "off"
    assert g.values() == {} and fp.closed and not g.is_open
    setting["v"] = "any"                       # reopens right away, no retry wait
    fp2 = FakePdh(amd_reads())
    pdh.result = fp2
    assert g.values()["gn"] == "AMD" and pdh.calls == 2
    setting["v"] = "nvidia"                    # no NVIDIA here
    assert g.values() == {} and fp2.closed


def test_mode_provider_error_counts_as_any():
    def broken():
        raise KeyError("settings")
    g = gpu(broken, adapters=Factory([AMD]), pdh=Factory(FakePdh(amd_reads())))
    assert g.values()["gn"] == "AMD"


# ---- GPU: failures ------------------------------------------------------------------
def test_dxgi_failure_falls_back_to_busiest_pdh_luid():
    other = (0, 0x7777)
    reads = [([], [mem(AMD.luid, 4800 * MB), mem(other, 0)]),
             ([eng(AMD.luid, 1, "3D", 3.0), eng(other, 1, "3D", 20.0)],
              [mem(AMD.luid, 4800 * MB), mem(other, 0)])]
    g = gpu(adapters=Factory(OSError("no DXGI")), pdh=Factory(FakePdh(reads)))
    # Idle: ties broken by VRAM in use; no total without DXGI.
    assert g.values() == {"g": 0, "gm": 4800, "gn": "GPU"}
    assert g.values() == {"g": 20, "gm": 0, "gn": "GPU"}


def test_pdh_open_failure_omits_keys_and_retries_later():
    clock = Clock()
    pdh = Factory(OSError("PdhOpenQuery failed"))
    g = gpu(adapters=Factory([AMD]), pdh=pdh, clock=clock)
    assert g.values() == {} and pdh.calls == 1
    clock.t += 10
    assert g.values() == {} and pdh.calls == 1             # not every 2 s
    clock.t += sysstats.GpuStats.RETRY
    pdh.result = FakePdh(amd_reads())
    assert g.values()["gn"] == "AMD" and pdh.calls == 2


def test_dxgi_raising_inside_open_and_pdh_read_errors_never_escape():
    fp = FakePdh([([], [mem(AMD.luid, MB)]), OSError("collect failed"),
                  ([eng(AMD.luid, 1, "3D", 5.0)], [mem(AMD.luid, MB)])])
    g = gpu(adapters=Factory([AMD]), pdh=Factory(fp))
    assert g.values()["gm"] == 1
    assert g.values() == {}                                 # this sample: no GPU
    assert g.values()["g"] == 5 and g.is_open               # query kept
    # Warm-up read failing closes the half-open query again.
    bad = FakePdh([OSError("first collect failed")])
    g = gpu(adapters=Factory([AMD]), pdh=Factory(bad))
    assert g.values() == {} and bad.closed and not g.is_open
    # Garbage from the counters must not escape either (and closes the query).
    junk = FakePdh([([eng(AMD.luid, 1, "3D", "nan?")], [])])
    g = gpu(adapters=Factory([AMD]), pdh=Factory(junk))
    assert g.values() == {} and junk.closed and not g.is_open


def test_adapter_vanishing_from_pdh_triggers_reenumeration():
    clock = Clock()
    gone = ([], [mem((0, 0x9999), MB)])
    fp = FakePdh(amd_reads()[:1] + [gone])
    adapters, pdh = Factory([AMD]), Factory(fp)
    g = gpu(adapters=adapters, pdh=pdh, clock=clock)
    assert g.values()["gn"] == "AMD"
    for _ in range(sysstats.GpuStats.MAX_MISSES):
        assert g.values() == {}
    assert fp.closed and not g.is_open
    clock.t += 5
    pdh.result = FakePdh(amd_reads())
    assert g.values()["gn"] == "AMD" and adapters.calls == 2


def test_real_layers_off_windows_raise_cleanly(monkeypatch):
    monkeypatch.setattr(sysstats.sys, "platform", "linux")
    with pytest.raises(OSError):
        sysstats.dxgi_adapters()
    with pytest.raises(OSError):
        sysstats.PdhGpu()
    monkeypatch.setitem(sys.modules, "pynvml", None)
    g = sysstats.GpuStats("any", clock=Clock())
    assert g.values() == {}


# ---- message --------------------------------------------------------------------------
def test_32_cores_fit_a_small_mtu_after_shrinking(monkeypatch):
    st, _, _ = make(monkeypatch, cores=[100.0] * 32, nvml=FakeNvml(), adapters=lambda: [NV])
    stats = st.sample()
    assert stats["gn"] == "NVIDIA"
    lk = cm.ClawdmeterLink(system_provider=lambda: stats)
    lk._proto = 2
    lk._mtu = 247
    roomy = json.loads(lk._system_message())
    assert len(roomy["cc"]) == 24
    lk._mtu = 160
    data = lk._system_message()
    assert len(data) <= 157
    msg = json.loads(data)
    assert len(msg["cc"]) in (12, 6, 3) and all(v == 100 for v in msg["cc"])
    assert {k: v for k, v in msg.items() if k != "cc"} == \
        {"k": "sy", **{k: v for k, v in stats.items() if k != "cc"}}


def test_worst_case_message_keeps_every_field_at_default_mtu(monkeypatch):
    """Biggest numbers in every field, 24 cores at 100 %, gn present: one
    247-byte packet; only "cc" shrinks (it already did before "gn" existed),
    a typical idle-ish sample keeps all 24 cores."""
    big = Adapter("x", 0x10DE, 98304 * MB, (0, 1))
    reads = [([eng(big.luid, 1, "3D", 100.0)], [mem(big.luid, 98304 * MB)])]
    st, ps, _ = make(monkeypatch, cores=[100.0] * 24, adapters=lambda: [big],
                     pdh=Factory(FakePdh(reads)))
    stats = st.sample()
    stats.update({"m": 262144, "mt": 262144, "du": 99999, "dt": 99999,
                  "dr": 9999999, "dw": 9999999, "nd": 9999999, "nu": 9999999})
    lk = cm.ClawdmeterLink(system_provider=lambda: stats)
    lk._proto, lk._mtu = 2, 247
    data = lk._system_message()
    msg = json.loads(data)
    assert len(data) <= 244 and len(msg["cc"]) == 12
    assert list(msg) == ["k", "c", "cc", "m", "mt", "g", "gm", "gt", "gn",
                         "dn", "du", "dt", "dr", "dw", "nd", "nu"]
    assert {k: v for k, v in msg.items() if k != "cc"} ==         {"k": "sy", **{k: v for k, v in stats.items() if k != "cc"}}
    # Typical: cores mostly single digits, 128 GB machine.
    stats.update({"cc": [7, 3, 12, 5] * 6, "c": 6, "m": 40960, "mt": 131072,
                  "g": 9, "gm": 4800, "gt": 32590, "gn": "AMD", "du": 812, "dt": 3725,
                  "dr": 120, "dw": 2048, "nd": 35, "nu": 12})
    data = lk._system_message()
    assert len(data) <= 244 and len(json.loads(data)["cc"]) == 24


# ---- app wiring ------------------------------------------------------------------------
def test_api_passes_the_live_setting(monkeypatch):
    import claude_sessions as cs
    assert cs.DEFAULT_SETTINGS["clawdmeter_gpu"] == "any"
    ps = FakePsutil([10])
    monkeypatch.setattr(sysstats, "psutil", ps)
    seen = []

    class Spy(sysstats.GpuStats):
        def _open(self, mode):
            seen.append(mode)

    monkeypatch.setattr(sysstats, "GpuStats", Spy)
    api = cs.Api.__new__(cs.Api)
    api.settings = {"clawdmeter_gpu": "nvidia"}
    s = api._clawd_system()
    assert "c" in s and seen == ["nvidia"]
    api.settings["clawdmeter_gpu"] = "off"
    assert "g" not in api._clawd_system() and seen == ["nvidia"]
    api.settings["clawdmeter_gpu"] = "any"
    api._clawd_system()
    assert seen == ["nvidia", "any"]
