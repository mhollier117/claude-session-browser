"""PC system stats for the Clawdmeter "System" screen (sysstats.py)."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import clawdmeter as cm  # noqa: E402
import sysstats  # noqa: E402

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
    def __init__(self, fail_init=False, count=1):
        self.fail_init, self.count = fail_init, count

    def nvmlInit(self):
        if self.fail_init:
            raise RuntimeError("NVML Shared Library Not Found")

    def nvmlDeviceGetCount(self):
        return self.count

    def nvmlDeviceGetHandleByIndex(self, i):
        assert i == 0
        return "gpu0"

    def nvmlDeviceGetUtilizationRates(self, h):
        return NS(gpu=37, memory=5)

    def nvmlDeviceGetMemoryInfo(self, h):
        return NS(total=24 * GB, used=int(3.5 * GB), free=0)


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def make(monkeypatch, cores=(10, 20, 30, 40), nvml=None):
    ps = FakePsutil(cores)
    monkeypatch.setattr(sysstats, "psutil", ps)
    if nvml is None:
        monkeypatch.setitem(sys.modules, "pynvml", None)   # import fails
    else:
        monkeypatch.setitem(sys.modules, "pynvml", nvml)
    monkeypatch.setenv("SystemDrive", "C:")
    clock = Clock()
    return sysstats.SystemStats(clock=clock), ps, clock


def test_sample_fields_and_units(monkeypatch):
    st, ps, _ = make(monkeypatch, nvml=FakeNvml())
    s = st.sample()
    assert list(s) == ["c", "cc", "m", "mt", "g", "gm", "gt",
                       "dn", "du", "dt", "dr", "dw", "nd", "nu"]
    assert all(isinstance(v, int) for k, v in s.items() if k not in ("cc", "dn"))
    assert s["c"] == 25 and s["cc"] == [10, 20, 30, 40]
    assert s["m"] == 12 * 1024 and s["mt"] == 32 * 1024
    assert s["g"] == 37 and s["gm"] == 3584 and s["gt"] == 24576
    if sys.platform == "win32":
        assert s["dn"] == "C:" and ps.paths[-1] == "C:\\"
    assert s["du"] == 400 and s["dt"] == 931
    # First call: no previous counters, so rates are zero.
    assert [s[k] for k in ("dr", "dw", "nd", "nu")] == [0, 0, 0, 0]


def test_gpu_absent_omits_keys(monkeypatch):
    for nvml in (None, FakeNvml(fail_init=True), FakeNvml(count=0)):
        st, _, _ = make(monkeypatch, nvml=nvml)
        s = st.sample()
        assert not st.has_gpu
        assert not {"g", "gm", "gt"} & set(s)
        assert {"c", "cc", "m", "mt", "dn", "du", "dt", "dr", "dw", "nd", "nu"} <= set(s)


def test_gpu_read_error_skips_gpu_for_that_sample(monkeypatch):
    nv = FakeNvml()
    st, _, _ = make(monkeypatch, nvml=nv)
    nv.nvmlDeviceGetMemoryInfo = lambda h: (_ for _ in ()).throw(RuntimeError("gone"))
    assert "g" not in st.sample()


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


def test_32_cores_fit_a_small_mtu_after_shrinking(monkeypatch):
    st, _, _ = make(monkeypatch, cores=[100.0] * 32, nvml=FakeNvml())
    stats = st.sample()
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
