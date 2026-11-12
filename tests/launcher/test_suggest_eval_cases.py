import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import suggest_eval  # noqa: E402


def _write(tmp_path, data):
    p = tmp_path / "cases.json"
    p.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return str(p)


def test_cases_file_loads_good_cases(tmp_path):
    got = suggest_eval._load_cases(_write(tmp_path, [
        {"name": "x y", "accept": [["work\\net", "PLACE", False], ["home", None, "prefix"]], "in_prompt": True}]))
    assert got == [("x y", [("work\\net", "PLACE", False), ("home", None, "prefix")], "", True)]


@pytest.mark.parametrize("data, why", [
    ("{not json", "not valid JSON"),
    ({"name": "x"}, "top level must be a list"),
    (["x"], "case 1 is not an object"),
    ([{"accept": [["a", "PLACE", False]]}], "case 1 needs a non-empty string 'name'"),
    ([{"name": "x", "accept": []}], "case 1 needs a non-empty list 'accept'"),
    ([{"name": "x", "accept": [["a", "PLACE"]]}], "case 1 accept entry 1 must be"),
    ([{"name": "x", "accept": [["a", "FOLDER", False]]}], "case 1 accept entry 1 must be"),
    ([{"name": "x", "accept": [["a", "PLACE", False]], "in_prompt": "yes"}], "'in_prompt' a boolean"),
])
def test_bad_cases_file_exits_with_one_line(tmp_path, data, why):
    path = _write(tmp_path, data)
    with pytest.raises(SystemExit) as e:
        suggest_eval._load_cases(path)
    msg = str(e.value)
    assert msg.startswith(f"--cases {path}: ") and why in msg and "\n" not in msg


def test_widened_and_in_prompt_cases():
    cases = {n: acc for n, acc, _note in suggest_eval.CASES}
    assert ("cc\\claude_improvements\\csb-launcher", "PROJECT", False) in cases["claude session browser launcher review"]
    assert ("work\\rewst\\integrations\\keepit", "PROJECT", False) in cases["keepit backup api paging"]
    assert "razer app decompile" in suggest_eval.IN_PROMPT