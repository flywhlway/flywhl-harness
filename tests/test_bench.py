"""基准本身的离线测试。sdist 不带 bench/，解包后跑测试时跳过。

任务校验会执行检查脚本和参考答案，所以放在这里：经 scripts/safe_pytest.py 运行，在系统沙箱里跑。
"""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("bench.taskset", reason="bench/ 不在 sdist 里")

from bench import redact, report  # noqa: E402
from bench.common import (  # noqa: E402
    BudgetExceeded, Ledger, append_jsonl, load_plan, read_jsonl, require_disposable, require_sandbox, unexpected_mounts,
)
from bench.q2 import run_q2, schedule, trial_env  # noqa: E402
from bench.q3 import run_q3  # noqa: E402
from bench.rehearsal import problems, script_for  # noqa: E402
from bench.taskset import CATEGORIES, load_tasks, run_check, validate_task  # noqa: E402
from flywhl_harness import Store  # noqa: E402
from flywhl_harness.observe.evals import mcnemar_exact  # noqa: E402
from flywhl_harness.tools.sandbox import Detected  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TASKS = load_tasks()


@pytest.mark.parametrize("task", TASKS, ids=[t.id for t in TASKS])
def test_task_is_valid(task):
    """夹具原样不通过，参考答案能通过。"""
    assert validate_task(task) == []


def test_log_fixtures_survive_a_global_ignore_rule(tmp_path):
    """镜像只打包提交了的文件。夹具被全局规则挡住时，干净克隆里的题目没了输入，校验却可能照样通过。"""
    if not (ROOT / ".git").exists():
        pytest.skip("不在 git 仓库里")
    excludes = tmp_path / "ignore"
    excludes.write_text("*.log\n", encoding="utf-8")
    out = subprocess.run(["git", "-c", f"core.excludesFile={excludes}", "ls-files", "-o", "-i", "--exclude-standard", "bench/tasks"],
                         cwd=ROOT, capture_output=True, text=True, check=True)
    assert [f for f in out.stdout.splitlines() if "__pycache__" not in f] == []


def test_check_gets_a_temp_dir_outside_the_workspace(tmp_path):
    (tmp_path / "task").mkdir()
    (tmp_path / "ws").mkdir()
    check = tmp_path / "task" / "check.py"
    check.write_text("import os, sys, tempfile\nprint(tempfile.gettempdir())\nsys.exit(0 if os.path.isdir(os.environ['TMPDIR']) else 1)\n", encoding="utf-8")
    task = next(iter(TASKS))
    passed, note = run_check(replace(task, dir=tmp_path / "task"), tmp_path / "ws", home=tmp_path)
    assert passed
    assert not Path(note).resolve().is_relative_to((tmp_path / "ws").resolve())


def test_every_category_is_declared():
    assert {t.category for t in TASKS} <= set(CATEGORIES)


# ---------------------------------------------------------------------- 方案
def test_plan_and_methodology_agree():
    text = (ROOT / "docs" / "benchmark" / "methodology.md").read_text(encoding="utf-8")
    plan = load_plan()
    for model in [*plan["q1"]["models"], plan["q2"]["model"]]:
        assert f"`{model}`" in text
    assert f"总额上限 {plan['budget']['total_usd']:.0f} 美元，每家上限 {plan['budget']['per_model_usd']:.0f} 美元" in text
    assert f"{plan['q2']['max_turns']} 轮，{plan['q2']['max_cost_usd']:.2f} 美元，{plan['q2']['timeout_seconds']} 秒" in text
    assert plan["q1"]["trials"] == plan["q2"]["trials"] == 3 and "每组 3 次" in text
    assert plan["q2"]["configs"]["A"]["sandbox"] == "auto" and plan["q2"]["configs"]["B"]["sandbox"] == "off"


def test_q1_scenarios_are_the_24_generic_ones():
    scenarios = load_plan()["q1"]["scenarios"]
    assert len(scenarios) == len(set(scenarios)) == 24
    for node in scenarios:
        path, name = node.split("::")
        assert f"def {name}(" in (ROOT / path).read_text(encoding="utf-8"), node
    needs_other_keys = ("across_providers", "cross_provider", "configured_fallback", "other_providers", "anthropic_protocol")
    assert not [s for s in scenarios if any(k in s for k in needs_other_keys)]


def test_pilot_tasks_exist_and_cover_a_sandbox_sensitive_one():
    by_id = {t.id: t for t in TASKS}
    pilot = [by_id[name] for name in load_plan()["q2"]["pilot_tasks"]]
    assert any(t.sandbox_sensitive for t in pilot) and not all(t.sandbox_sensitive for t in pilot)


def test_power_statement_in_the_methodology():
    assert mcnemar_exact([False] * 6, [True] * 6)["p_value"] < 0.05
    assert mcnemar_exact([False] * 5, [True] * 5)["p_value"] > 0.05
    assert mcnemar_exact([False] * 8 + [True], [True] * 8 + [False])["p_value"] < 0.05


def test_task_set_shape_matches_the_methodology():
    if len(TASKS) < 30:
        pytest.skip("任务还没写全")
    counts = Counter(t.category for t in TASKS)
    assert counts == {"fix": 6, "feature": 5, "refactor": 3, "test": 3, "data": 4, "repo": 3, "environment": 6}
    assert sum(t.sandbox_sensitive for t in TASKS) == 5
    assert all(t.category == "environment" for t in TASKS if t.sandbox_sensitive)


def test_schedule_keeps_pairs_adjacent_and_is_repeatable():
    order = schedule(TASKS, 3, 20261105)
    assert order == schedule(TASKS, 3, 20261105)
    assert len(order) == 6 * len(TASKS)
    for first, second in zip(order[::2], order[1::2]):
        assert first[:2] == second[:2] and {first[2], second[2]} == {"A", "B"}
    assert len({o[2] for o in order[::2]}) == 2, "先跑的不能总是同一种配置"


# ---------------------------------------------------------------------- 防线
def test_only_system_mounts_are_expected():
    info = ("1 0 0:1 / / rw - overlay overlay rw\n2 1 0:2 / /proc rw - proc proc rw\n"
            "3 1 0:3 / /etc/hosts rw - ext4 /dev/vda rw\n4 1 0:4 /Users/me /work rw - virtiofs mac rw\n")
    assert unexpected_mounts(info) == ["/work"]


def test_real_runs_refuse_to_start_on_the_host(monkeypatch):
    monkeypatch.delenv("FLYWHL_BENCH_DISPOSABLE", raising=False)
    with pytest.raises(SystemExit, match="一次性容器"):
        require_disposable()


def test_real_runs_refuse_to_start_without_a_working_sandbox(monkeypatch):
    monkeypatch.setattr("bench.common.probe", lambda: Detected("", "user namespace 被禁用"))
    with pytest.raises(SystemExit, match="user namespace"):
        require_sandbox()


def test_rehearsal_flags_results_that_prove_nothing():
    def row(task, config, passed, sensitive):
        return {"task": task, "config": config, "passed": passed, "sandbox_sensitive": sensitive, "note": ""}
    good = [row("t", "A", True, False), row("t", "B", True, False), row("s", "A", False, True), row("s", "B", True, True)]
    assert problems(good, []) == []
    assert problems([row("t", "A", False, False)], []) != []         # 普通任务不该败
    assert problems([row("s", "A", True, True)], []) != []           # 敏感任务开着沙箱不该过
    assert problems([row("s", "B", False, True)], []) != []          # 关了沙箱应当能过
    assert problems([], [{"task": "s", "config": "A", "trajectory": {"text": "沙箱不可用，命令未执行"}}]) != []


def test_ledger_stops_at_both_caps_and_survives_a_restart(tmp_path):
    path = tmp_path / "spent.json"
    led = Ledger(path, total_usd=1.0, per_model_usd=0.6)
    led.charge("a", 0.6)
    with pytest.raises(BudgetExceeded):
        led.check("a")
    led.check("b")
    led = Ledger(path, total_usd=1.0, per_model_usd=0.6)
    led.charge("b", 0.5)
    with pytest.raises(BudgetExceeded):
        led.check("c")


def test_redaction_removes_keys_and_paths(tmp_path):
    raw, public = tmp_path / "raw", tmp_path / "public"
    (raw / "q1").mkdir(parents=True)
    fake = "not-a-real-key-7f3a9c"
    (raw / "q1" / "a.jsonl").write_text(f'{{"t": "{fake} sk-abcdefghijklmnopqrstuv /Users/someone/x /home/bench/repo"}}\n')
    (raw / "harness.db").write_bytes(b"\x00binary")
    hits = redact.redact_tree(raw, public, [fake])
    text = (public / "q1" / "a.jsonl").read_text()
    assert fake not in text and "sk-abc" not in text and "/Users/someone" not in text
    assert "/home/bench/repo" in text, "容器里的路径不算隐私，换掉反而让回放对不上"
    assert hits == {"q1/a.jsonl": 3}
    assert not (public / "harness.db").exists(), "认不出的文件不发布"
    assert json.loads(text)
    assert redact.leftovers(public, [fake]) == []
    (public / "b.txt").write_text(fake)
    assert redact.leftovers(public, [fake]) == ["b.txt"]


@pytest.mark.parametrize("text", [
    json.dumps({"o": "第一行\n/home/alice/y"}), "-I/home/alice/include", "/mnt/c/Users/alice/y", "file:///Users/alice/y",
])
def test_redaction_catches_paths_after_any_prefix(text):
    """扫的是序列化后的文本：路径前面可能是转义符、编译选项或挂载点。"""
    assert "alice" not in redact.redact_text(text, [])[0]


def test_trial_paths_do_not_trip_the_path_rule(tmp_path):
    with trial_env(tmp_path, None) as home:
        assert redact.PATTERNS["path"].search(f"/work/bench-x/{home.name}/reports/a.txt") is None


def test_short_env_values_are_not_treated_as_secrets():
    assert redact.env_secrets({"API_KEY": "abcdefghij", "TOKEN_LIMIT": "100", "PATH": "/usr/bin:/usr/local/bin"}) == ["abcdefghij"]


# ---------------------------------------------------------------------- 报告
def _q1_rows(out: Path, model: str, rows: list[dict]) -> None:
    append_jsonl(out / "q1" / model / "trial-1" / "results.jsonl", rows)


def _q2_row(task: str, config: str, trial: int, passed: bool, *, sensitive: bool = False, seconds: float = 10.0) -> dict:
    return {"task": task, "config": config, "trial": trial, "passed": passed, "sandbox_sensitive": sensitive, "seconds": seconds,
            "status": "completed", "note": "", "reason": "", "cost_usd": 0.01}


def test_report_excludes_only_network_failures_from_the_denominator(tmp_path):
    usage = {"input_tokens": 100, "output_tokens": 10, "cache_read_tokens": 300, "cost_usd": 0.01}
    _q1_rows(tmp_path, "m", [
        {"nodeid": "t::ok", "outcome": "passed", "seconds": 1.0, "runs": [usage]},
        {"nodeid": "t::net", "outcome": "failed", "seconds": 1.0, "detail": "超时"},
        {"nodeid": "t::model", "outcome": "failed", "seconds": 1.0, "detail": "没照做"},
        {"nodeid": "t::key", "outcome": "skipped", "seconds": 0.0},
    ])
    append_jsonl(tmp_path / "classifications.jsonl", [{"id": "q1/m/trial-1/t::net", "category": "network_env"},
                                                      {"id": "q1/m/trial-1/t::model", "category": "model_random"}])
    rep = report.build(tmp_path)
    row = rep["q1"][0]
    assert (row["passed"], row["n"], row["skipped"], row["excluded"]) == (1, 2, 1, 1)
    assert row["cache_hit_rate"] == 0.75 and rep["unclassified"] == []


def test_report_pairs_tasks_by_majority(tmp_path):
    rows = []
    for t in range(1, 4):
        rows += [_q2_row("same", "A", t, True), _q2_row("same", "B", t, True)]
        rows += [_q2_row("a-worse", "A", t, t == 1), _q2_row("a-worse", "B", t, True)]   # A 1/3 不过，B 3/3 过
        rows += [_q2_row("boundary", "A", t, False, sensitive=True), _q2_row("boundary", "B", t, True, sensitive=True)]
    append_jsonl(tmp_path / "q2" / "results.jsonl", rows)
    q2 = report.build(tmp_path)["q2"]
    assert q2["paired_general"] == {"tasks": 2, "a_passed": 1, "b_passed": 2, "a_only": 0, "b_only": 1, "p_value": 1.0}
    assert q2["paired_all"]["b_only"] == 2
    assert q2["sensitive"] == {"boundary": {"A": "0/3", "B": "3/3"}}
    assert q2["configs"]["A"]["general"]["passed"] == 4 and q2["configs"]["A"]["premature_stop"] == 5


def test_site_pages_refuse_unclassified_failures_and_rehearsals(tmp_path):
    append_jsonl(tmp_path / "q2" / "results.jsonl", [_q2_row("x", "A", 1, False), _q2_row("x", "B", 1, True)])
    with pytest.raises(SystemExit, match="没有归类"):
        report.write_pages(report.build(tmp_path), tmp_path / "docs")
    append_jsonl(tmp_path / "classifications.jsonl", [{"id": "q2/x/A/1", "category": "model_random"}])
    (tmp_path / "manifest.json").write_text('{"rehearsal": true}')
    with pytest.raises(SystemExit, match="彩排"):
        report.write_pages(report.build(tmp_path), tmp_path / "docs")
    (tmp_path / "manifest.json").write_text('{"rehearsal": false, "harness_version": "9.9"}')
    pages = report.write_pages(report.build(tmp_path), tmp_path / "docs")
    assert "9.9" in pages[0].read_text(encoding="utf-8") and pages[1].read_text(encoding="utf-8").startswith("# Benchmark results")


def test_unknown_failure_class_is_rejected(tmp_path):
    append_jsonl(tmp_path / "classifications.jsonl", [{"id": "x", "category": "说不清"}])
    with pytest.raises(ValueError, match="不认识"):
        report.build(tmp_path)


# ---------------------------------------------------------------------- 流水线
async def test_pipeline_runs_end_to_end_with_a_scripted_model(tmp_path, monkeypatch):
    """脚本化模型照参考答案做题：结果、轨迹、回放、脱敏、报告走一遍。沙箱敏感题的区分度在容器彩排里验证。"""
    monkeypatch.setenv("BENCH_WORK", str(tmp_path))      # 镜像里默认是 /work，测试沙箱里只读
    plan = load_plan()
    plan["q2"] = {**plan["q2"], "trials": 1}
    tasks = [t for t in TASKS if t.id == "fix-pagination"]
    out = tmp_path / "raw"
    await run_q2(plan, out, tasks=tasks, provider_for=script_for, model="scripted")
    rows = list(read_jsonl(out / "q2" / "results.jsonl"))
    assert [(r["config"], r["passed"], r["status"]) for r in sorted(rows, key=lambda r: r["config"])] == \
        [("A", True, "completed"), ("B", True, "completed")]

    await run_q2(plan, out, tasks=tasks, provider_for=script_for, model="scripted")   # 接着跑不重复
    assert len(list(read_jsonl(out / "q2" / "results.jsonl"))) == 2

    assert await run_q3(out) == {"trajectories": 2, "divergent": 0}
    assert redact.publish(out, tmp_path / "public") == {}
    assert report.build(tmp_path / "public")["q3"]["divergent"] == 0


def test_live_fixture_exports_every_run(tmp_path, monkeypatch):
    live = pytest.importorskip("tests.live.conftest")
    out = tmp_path / "artifacts"
    out.mkdir()
    monkeypatch.setattr(live, "_ARTIFACTS", str(out))
    store = Store(tmp_path / "t" / "harness.db") if (tmp_path / "t").mkdir() is None else None
    s = store.create_session("x", str(tmp_path), meta={"tools": []})
    store.create_run(s.id, "题目")
    store.close()
    usage = live._save_trajectories("t::x", tmp_path / "t")
    rows = list(read_jsonl(out / "trajectories.jsonl"))
    assert len(usage) == 1 and rows[0]["nodeid"] == "t::x" and rows[0]["trajectory"]["format"] == "flywhl-trajectory/1"


def test_site_pages_list_each_failure_and_the_deviations(tmp_path):
    append_jsonl(tmp_path / "q2" / "results.jsonl", [_q2_row("t", "A", 1, False), _q2_row("t", "B", 1, True)])
    (tmp_path / "classifications.jsonl").write_text(json.dumps(
        {"id": "q2/t/A/1", "category": "sandbox_boundary", "note": "被拦了", "note_en": "blocked"}) + "\n", encoding="utf-8")
    (tmp_path / "deviations.json").write_text(json.dumps([{"zh": "作废一次", "en": "voided one"}]), encoding="utf-8")
    zh, en = report.write_pages(report.build(tmp_path), tmp_path / "docs")
    assert "`q2/t/A/1` | 沙箱边界 | 被拦了" in zh.read_text(encoding="utf-8") and "作废一次" in zh.read_text(encoding="utf-8")
    assert "`q2/t/A/1` | sandbox boundary | blocked" in en.read_text(encoding="utf-8") and "voided one" in en.read_text(encoding="utf-8")


def test_container_reaps_orphans():
    """没有 init 时 Python 是 PID 1，退出的 bwrap 进程没人回收，正式运行因此崩过一次。"""
    assert "docker run --init " in (Path(__file__).parent.parent / "bench" / "run.sh").read_text(encoding="utf-8")
