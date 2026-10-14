"""08 · 轨迹、回放与评测：导出轨迹（含 OTel span）→ 确定性回放 → 发现分叉 → 带置信区间的评测。

运行：python examples/08_replay_eval.py
"""

import asyncio
import zlib
from pathlib import Path

from _common import harness, say, temp_workspace, title

from flywhl_harness import ScriptedProvider
from flywhl_harness.observe.evals import EvalCase, mcnemar_exact, run_eval, wilson_interval
from flywhl_harness.observe.replay import replay_run
from flywhl_harness.observe.trace import export_trajectory
from flywhl_harness.tools.demo import FakeNotifyService

SCRIPT = [
    {"tool_calls": [{"name": "read", "args": {"path": "todo.txt"}}]},
    {"tool_calls": [{"name": "send_notification", "args": {"to": "me", "message": "今天有 2 件事"}}]},
    "已提醒你今天的 2 件事。",
]


async def main() -> None:
    title("录制一次运行")
    ws = temp_workspace("08")
    (ws / "todo.txt").write_text("买菜\n写周报\n")
    h = harness(ws, ScriptedProvider(SCRIPT))
    res = await h.run("看看待办并提醒我")
    traj = export_trajectory(h.store, res.run_id)
    say(f"轨迹：{len(traj['messages'])} 条消息、{len(traj['steps'])} 个工具 step、{len(traj['events'])} 个事件、"
        f"{len(traj['fingerprints'])} 个上下文指纹")
    for sp in traj["spans"]:
        print(f"  span {sp['name']:<32} {sp['attributes'].get('gen_ai.operation.name')}")

    title("确定性回放：不调用模型、不执行副作用")
    svc = FakeNotifyService(h.data_dir / "external_notify.db")
    before = svc.count()
    rep = await replay_run(h.store, res.run_id, config=h.config, base_tools=h.tools)
    say(f"工具序列一致={rep.same_tool_sequence}，最终答案一致={rep.same_final_text}，分叉={rep.divergence}，"
        f"外部通知数 {before} → {svc.count()}（没有重放副作用）")

    title("改了系统提示词再回放：定位分叉点")
    s = h.store.get_session(res.session_id)
    h.store.update_session(s.id, meta={**s.meta, "system_prompt": s.meta["system_prompt"] + "\n（新增一条规则）"})
    rep2 = await replay_run(h.store, res.run_id, config=h.config, base_tools=h.tools)
    say(f"分叉：{rep2.divergence}；{rep2.notes}")

    title("评测：以产物判定成功，报告 Wilson 95% 置信区间")

    def make_cases(n):
        cases = []
        for i in range(n):
            cases.append(EvalCase(f"case{i}", f"创建 out{i}.txt",
                                  check=lambda ws, r, i=i: ((ws / f"out{i}.txt").exists(), "产物存在"),
                                  setup=lambda ws: None))
        return cases

    async def factory_good(ws: Path):
        return harness(ws, ScriptedProvider([lambda req: {"tool_calls": [{"name": "write", "args": {
            "path": req.messages[0].text.split()[-1], "content": "ok"}}]}, "完成"]))

    async def factory_lazy(ws: Path):  # 只说“完成了”，不产出文件：典型的“虚报完成”
        state = {"n": 0}
        def turn(req):
            state["n"] += 1
            return "完成了！" if zlib.crc32(req.messages[0].text.encode()) % 3 else {"tool_calls": [{"name": "write", "args": {
                "path": req.messages[0].text.split()[-1], "content": "ok"}}]}
        return harness(ws, ScriptedProvider([turn, "完成"]))

    good = await run_eval(make_cases(12), factory_good)
    lazy = await run_eval(make_cases(12), factory_lazy)
    for name, rep in (("A 版本", good), ("B 版本", lazy)):
        s = rep.summary()
        print(f"  {name}：成功率 {s['success_rate']:.0%}  95% CI {s['ci95']}  过早停止率 {s['premature_stop_rate']:.0%}")
    test = mcnemar_exact([r.passed for r in good.results], [r.passed for r in lazy.results])
    print(f"  配对检验（McNemar 精确）：A 独赢 {test['a_only']}、B 独赢 {test['b_only']}，p = {test['p_value']:.4f}")
    lo, hi = wilson_interval(4, 5)
    say(f"小样本提醒：5 个用例过 4 个，Wilson 区间是 [{lo:.2f}, {hi:.2f}]——别把 80% 当成定论")
    await h.aclose()


if __name__ == "__main__":
    asyncio.run(main())
