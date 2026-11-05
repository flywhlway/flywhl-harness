"""从结果目录算出全部数字。这里只算不评论，结论的措辞在站点页面里由人写。

每个失败都要有归类，缺归类时只能出草稿，不能写进站点。彩排的结果同样不能写进站点。
"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

from flywhl_harness.observe.evals import mcnemar_exact, wilson_interval

from .common import CATEGORIES, NOT_COUNTED, read_jsonl

Q2_CATEGORIES = (*CATEGORIES, "sandbox_boundary")   # 只用于 Q2：沙箱按设计拦下了任务要做的事，这正是要测的代价，计入分母
Report = dict[str, Any]


def load_classes(out: Path) -> dict[str, dict[str, str]]:
    classes = {r["id"]: r for r in read_jsonl(out / "classifications.jsonl")}
    for rid, r in classes.items():
        if r["category"] not in Q2_CATEGORIES:
            raise ValueError(f"{rid} 的分类 {r['category']} 不认识")
    return classes


def _rate(k: int, n: int) -> dict[str, Any]:
    lo, hi = wilson_interval(k, n)
    return {"passed": k, "n": n, "rate": round(k / n, 4) if n else None, "ci95": [round(lo, 4), round(hi, 4)]}


def q1_summary(out: Path, classes: dict[str, dict[str, str]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    per_model, failures = [], []
    root = out / "q1"
    for model_dir in sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []:
        passed = counted = skipped = excluded = 0
        tokens = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0}
        cost = seconds = 0.0
        trials = sorted(model_dir.glob("trial-*"))
        for trial in trials:
            for row in read_jsonl(trial / "results.jsonl"):
                rid = f"q1/{model_dir.name}/{trial.name}/{row['nodeid']}"
                seconds += row["seconds"]
                for run in row.get("runs") or []:
                    cost += float(run.get("cost_usd") or 0)
                    for k in tokens:
                        tokens[k] += int(run.get(k) or 0)
                if row["outcome"] == "skipped":
                    skipped += 1
                elif row["outcome"] == "passed":
                    passed, counted = passed + 1, counted + 1
                else:
                    cls = classes.get(rid)
                    failures.append({"id": rid, "category": cls and cls["category"], "note": cls and cls.get("note", ""),
                                     "detail": row.get("detail", "")[-1500:]})
                    if cls and cls["category"] in NOT_COUNTED:
                        excluded += 1
                    else:
                        counted += 1
        prompt = tokens["input_tokens"] + tokens["cache_read_tokens"]
        per_model.append({"model": model_dir.name, "trials": len(trials), **_rate(passed, counted), "skipped": skipped,
                          "excluded": excluded, **tokens, "cache_hit_rate": round(tokens["cache_read_tokens"] / prompt, 4) if prompt else None,
                          "cost_usd": round(cost, 4), "seconds": round(seconds, 1)})
    return per_model, failures


def _majority(rows: list[dict[str, Any]]) -> bool | None:
    return sum(r["passed"] for r in rows) * 2 > len(rows) if rows else None


def _paired(tasks: dict[str, dict[str, list[dict[str, Any]]]], keep) -> dict[str, Any]:
    pairs = [(t, _majority(c["A"]), _majority(c["B"])) for t, c in tasks.items() if keep(t)]
    pairs = [p for p in pairs if p[1] is not None and p[2] is not None]
    a, b = [p[1] for p in pairs], [p[2] for p in pairs]
    return {"tasks": len(pairs), "a_passed": sum(a), "b_passed": sum(b), **mcnemar_exact(a, b)}


def q2_summary(out: Path, classes: dict[str, dict[str, str]]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    rows = list(read_jsonl(out / "q2" / "results.jsonl"))
    if not rows:
        return None, []
    failures, counted = [], []
    for r in rows:
        rid = f"q2/{r['task']}/{r['config']}/{r['trial']}"
        cls = classes.get(rid)
        if not r["passed"]:
            failures.append({"id": rid, "category": cls and cls["category"], "note": cls and cls.get("note", ""),
                             "detail": f"{r['status']}：{r['note'] or r['reason']}"})
        if r["passed"] or not (cls and cls["category"] in NOT_COUNTED):
            counted.append(r)
    sensitive = {r["task"] for r in rows if r["sandbox_sensitive"]}
    configs = {}
    for name in ("A", "B"):
        mine = [r for r in counted if r["config"] == name]
        done = [r for r in mine if r["status"] == "completed"]
        general = [r for r in mine if r["task"] not in sensitive]
        configs[name] = {"all": _rate(sum(r["passed"] for r in mine), len(mine)),
                         "general": _rate(sum(r["passed"] for r in general), len(general)),
                         "premature_stop": sum(1 for r in done if not r["passed"]),
                         "median_seconds": round(statistics.median(r["seconds"] for r in mine), 2) if mine else None,
                         "cost_usd": round(sum(r["cost_usd"] for r in mine), 4)}
    tasks: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: {"A": [], "B": []})
    for r in counted:
        tasks[r["task"]][r["config"]].append(r)
    both = {(r["task"], r["trial"]): r for r in rows if r["config"] == "A"}
    gaps = [both[(r["task"], r["trial"])]["seconds"] - r["seconds"] for r in rows
            if r["config"] == "B" and (r["task"], r["trial"]) in both and r["task"] not in sensitive]
    return {"configs": configs,
            "paired_general": _paired(tasks, lambda t: t not in sensitive),      # 主要分析：普通任务上的代价
            "paired_all": _paired(tasks, lambda t: True),                        # 次要分析
            "sensitive": {t: {c: f"{sum(r['passed'] for r in tasks[t][c])}/{len(tasks[t][c])}" for c in "AB"} for t in sorted(sensitive)},
            "median_extra_seconds_a_minus_b": round(statistics.median(gaps), 2) if gaps else None,
            "pairs": len(gaps)}, failures


def q3_summary(out: Path) -> dict[str, Any] | None:
    rows = list(read_jsonl(out / "q3" / "replays.jsonl"))
    if not rows:
        return None
    bad = [r for r in rows if not r["deterministic"]]
    return {"trajectories": len(rows), "divergent": len(bad), "divergences": [{"source": r["source"], "run_id": r["run_id"],
            "notes": r.get("notes") or [], "error": (r.get("error") or "")[-300:]} for r in bad]}


def build(out: Path) -> Report:
    classes = load_classes(out)
    manifest_path = out / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    q1, f1 = q1_summary(out, classes)
    q2, f2 = q2_summary(out, classes)
    failures = f1 + f2
    return {"manifest": manifest, "q1": q1, "q2": q2, "q3": q3_summary(out), "failures": failures,
            "unclassified": [f["id"] for f in failures if not f["category"]],
            "harness_bugs": [f["id"] for f in failures if f["category"] == "harness_bug"]}


def _pct(rate: float | None) -> str:
    return "—" if rate is None else f"{rate * 100:.1f}%"


def _ci(ci: list[float]) -> str:
    return f"{ci[0] * 100:.1f}%～{ci[1] * 100:.1f}%"


def render(report: Report, *, lang: str = "zh") -> str:
    """数字表格。中文、英文共用一份数据，所以两边的数字不会对不上。"""
    zh = lang == "zh"
    m = report["manifest"]
    out = ["<!-- 由 bench/report.py 生成，不要手改 -->", ""]
    out += [f"{'版本' if zh else 'Version'}: `{m.get('harness_version', '?')}` · commit `{m.get('commit', '?')}` · "
            f"plan `{m.get('plan_id', '?')}` · {m.get('started_at', '?')}", ""]
    if report["q1"]:
        out += ["## Q1", "", ("| 模型 | 通过率 | Wilson 95% 区间 | 跳过 | 不计入 | 缓存命中率 | token | 费用 | 耗时 |" if zh else
                              "| Model | Pass rate | Wilson 95% CI | Skipped | Excluded | Cache hit | Tokens | Cost | Time |"),
                "|---|---|---|---|---|---|---|---|---|"]
        for r in report["q1"]:
            tokens = r["input_tokens"] + r["output_tokens"] + r["cache_read_tokens"]
            out.append(f"| {r['model']} | {r['passed']}/{r['n']} ({_pct(r['rate'])}) | {_ci(r['ci95'])} | {r['skipped']} | {r['excluded']} "
                       f"| {_pct(r['cache_hit_rate'])} | {tokens:,} | ${r['cost_usd']:.4f} | {r['seconds']:.0f}s |")
        out.append("")
    if report["q2"]:
        q2 = report["q2"]
        out += ["## Q2", "", ("| 配置 | 全部任务 | 普通任务 | 普通任务区间 | 过早停止 | 耗时中位数 | 费用 |" if zh else
                              "| Config | All tasks | General tasks | General CI | Premature stops | Median time | Cost |"),
                "|---|---|---|---|---|---|---|"]
        for name in ("A", "B"):
            c = q2["configs"][name]
            out.append(f"| {name} | {c['all']['passed']}/{c['all']['n']} | {c['general']['passed']}/{c['general']['n']} | {_ci(c['general']['ci95'])} "
                       f"| {c['premature_stop']} | {c['median_seconds']}s | ${c['cost_usd']:.4f} |")
        p, q = q2["paired_general"], q2["paired_all"]
        out += ["", (f"配对检验（按任务多数决）：普通任务 {p['tasks']} 个，只有 A 过 {p['a_only']} 个，只有 B 过 {p['b_only']} 个，p = {p['p_value']:.4f}；"
                     f"全部任务 {q['tasks']} 个，只有 A 过 {q['a_only']} 个，只有 B 过 {q['b_only']} 个，p = {q['p_value']:.4f}。"
                     f"普通任务上 A 比 B 每次试验多用的时间，中位数 {q2['median_extra_seconds_a_minus_b']} 秒（{q2['pairs']} 对）。" if zh else
                     f"Paired test (per-task majority): {p['tasks']} general tasks, A only {p['a_only']}, B only {p['b_only']}, p = {p['p_value']:.4f}; "
                     f"{q['tasks']} tasks overall, A only {q['a_only']}, B only {q['b_only']}, p = {q['p_value']:.4f}. "
                     f"Median extra time per trial for A over B on general tasks: {q2['median_extra_seconds_a_minus_b']}s ({q2['pairs']} pairs)."), ""]
        if q2["sensitive"]:
            out += [("| 沙箱敏感任务 | A | B |" if zh else "| Sandbox-sensitive task | A | B |"), "|---|---|---|"]
            out += [f"| {t} | {v['A']} | {v['B']} |" for t, v in q2["sensitive"].items()]
            out.append("")
    if report["q3"]:
        q3 = report["q3"]
        out += ["## Q3", "", (f"回放 {q3['trajectories']} 条轨迹，分叉 {q3['divergent']} 条。" if zh else
                              f"Replayed {q3['trajectories']} trajectories, {q3['divergent']} diverged."), ""]
    counts: dict[str, int] = defaultdict(int)
    for f in report["failures"]:
        counts[f["category"] or "unclassified"] += 1
    if counts:
        out += [("## 失败分类" if zh else "## Failure classes"), "", "| " + ("分类" if zh else "Class") + " | " + ("个数" if zh else "Count") + " |", "|---|---|"]
        out += [f"| {k} | {v} |" for k, v in sorted(counts.items())]
        out.append("")
    return "\n".join(out)


def write_pages(report: Report, docs: Path) -> list[Path]:
    """写进站点。彩排数据、有未归类失败的数据都不允许。"""
    if report["manifest"].get("rehearsal"):
        raise SystemExit("拒绝写入：这是彩排的结果，不是真实模型的数据")
    if report["unclassified"]:
        raise SystemExit(f"拒绝写入：还有 {len(report['unclassified'])} 个失败没有归类")
    paths = [(docs / "benchmark" / "results.md", "zh"), (docs / "en" / "benchmark" / "results.md", "en")]
    for path, lang in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(("# 基准结果\n\n" if lang == "zh" else "# Benchmark results\n\n") + render(report, lang=lang), encoding="utf-8")
    return [p for p, _ in paths]
