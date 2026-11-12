import json

from launcher import flowlog


def _lines(env):
    return [json.loads(x) for x in (env.data_dir / "flow.log").read_text(encoding="utf-8").splitlines()]


def test_log_appends_one_json_line_per_event(fake_env):
    flowlog.log(fake_env, "start", folder="", kind=None, filter="", step="picker", name="x")
    flowlog.log(fake_env, "open", folder="work\\net", kind="child", filter="", step="picker")
    lines = _lines(fake_env)
    assert lines == [
        {"ts": fake_env.now(), "event": "start", "folder": "", "kind": None, "filter": "", "step": "picker",
         "name": "x"},
        {"ts": fake_env.now(), "event": "open", "folder": "work\\net", "kind": "child", "filter": "",
         "step": "picker"}]


def test_log_keeps_the_last_half_past_the_cap(fake_env, monkeypatch):
    monkeypatch.setattr(flowlog, "MAX_BYTES", 2000)
    for i in range(60):
        flowlog.log(fake_env, "open", n=i, pad="x" * 40)
    path = fake_env.data_dir / "flow.log"
    assert path.stat().st_size <= 2000
    lines = _lines(fake_env)  # every line still whole JSON
    assert lines[-1]["n"] == 59
    ns = [x["n"] for x in lines]
    assert ns == list(range(ns[0], 60)) and ns[0] > 0


def test_log_never_raises(fake_env):
    fake_env.data_dir.parent.mkdir(parents=True, exist_ok=True)
    fake_env.data_dir.write_text("not a folder", encoding="utf-8")  # mkdir and open both fail
    flowlog.log(fake_env, "cancel")
