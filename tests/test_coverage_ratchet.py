"""覆盖率棘轮的判定：只用合成的报告，不跑 coverage。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("coverage_ratchet", ROOT / "scripts" / "coverage_ratchet.py")
ratchet = importlib.util.module_from_spec(spec)
sys.modules["coverage_ratchet"] = ratchet
spec.loader.exec_module(ratchet)

MODULE = ratchet.CRITICAL[0]


def report(overall: float, **files: float) -> dict:
    return {"totals": {"percent_covered": overall},
            "files": {MODULE: {"summary": {"percent_covered": files.get("module", 90.0)}}, "src/flywhl_harness/other.py": {"summary": {"percent_covered": 1.0}}}}


def test_only_overall_and_critical_modules_are_compared():
    assert ratchet.read_current(report(82.0)) == {ratchet.OVERALL: 82.0, MODULE: 90.0}


@pytest.mark.parametrize(("now", "ok"), [(82.0, True), (81.5, True), (85.0, True), (81.49, False)])
def test_a_drop_past_the_tolerance_fails(now, ok):
    assert (ratchet.compare({ratchet.OVERALL: 82.0}, {ratchet.OVERALL: now}) == []) is ok


def test_a_module_that_drops_fails_even_if_the_total_rises():
    problems = ratchet.compare({ratchet.OVERALL: 82.0, MODULE: 90.0}, {ratchet.OVERALL: 90.0, MODULE: 89.0})
    assert len(problems) == 1 and MODULE in problems[0]


def test_a_module_missing_from_the_report_fails():
    assert "没有这一项" in ratchet.compare({MODULE: 90.0}, {})[0]


def test_update_only_goes_up():
    assert ratchet.raised({"a": 90.0, "b": 50.0}, {"a": 80.0, "b": 60.123, "c": 10.0}) == {"a": 90.0, "b": 60.12, "c": 10.0}


def test_critical_modules_exist():
    assert all((ROOT / m).is_file() for m in ratchet.CRITICAL)


def run(tmp_path: Path, rep: dict, *args: str, baseline: dict | None = None) -> int:
    (tmp_path / "r.json").write_text(json.dumps(rep), encoding="utf-8")
    base = tmp_path / "b.json"
    if baseline is not None:
        base.write_text(json.dumps(baseline), encoding="utf-8")
    return ratchet.main([str(tmp_path / "r.json"), "--baseline", str(base), *args])


def test_exit_codes(tmp_path, capsys):
    assert run(tmp_path, report(82.0), baseline={ratchet.OVERALL: 82.0}) == 0
    assert run(tmp_path, report(80.0), baseline={ratchet.OVERALL: 82.0}) == 1
    assert "低于基线" in capsys.readouterr().out


def test_a_missing_baseline_fails_but_can_be_written(tmp_path):
    assert run(tmp_path, report(82.0)) == 1
    assert run(tmp_path, report(82.0), "--update") == 0
    assert json.loads((tmp_path / "b.json").read_text(encoding="utf-8"))[ratchet.OVERALL] == 82.0
    assert run(tmp_path, report(82.0)) == 0
