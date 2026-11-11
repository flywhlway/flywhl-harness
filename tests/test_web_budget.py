"""联网计数与费用：进 checkpoint、run.result 与工具批次后的 usage 快照；费用计入 max_cost_usd；子预算往上传。"""

from __future__ import annotations

from flywhl_harness.agent.budget import Budget
from flywhl_harness.config import BudgetConfig

from .webkit import DS_SEARCH, Net, ds_search, make_web_harness, search_call


async def test_counters_land_in_checkpoint_result_and_usage_snapshots(ws):
    net = Net().on("POST", DS_SEARCH, ds_search())
    h = make_web_harness(ws, [search_call("a"), search_call("b"), "好"], net)
    res = await h.run("x")
    run = h.store.get_run(res.run_id)
    assert run.result["web_searches"] == 2 and run.result["web_fetches"] == 0
    assert run.result["tool_cost_usd"] == 0.0015
    assert run.checkpoint["budget"]["web_searches"] == 2 and run.checkpoint["budget"]["tool_cost_usd"] == 0.0015
    snaps = [e.data for e in res.events if e.type == "usage"]
    after_tools = [s for s in snaps if s.get("web_searches")]
    assert after_tools[0]["web_searches"] == 1 and after_tools[-1]["web_searches"] == 2   # 每个工具批次之后都更新
    assert after_tools[-1]["max_web_searches"] == 20 and after_tools[-1]["max_web_fetches"] == 10


async def test_search_cost_counts_toward_max_cost(ws):
    net = Net().on("POST", DS_SEARCH, ds_search())
    h = make_web_harness(ws, [search_call("a"), search_call("b"), search_call("c"), "好"], net,
                         budget__max_cost_usd=1.5, web__backend={"deepseek": {"price_per_call": 1.0}})
    res = await h.run("x")
    assert "费用" in res.reason and "含联网" in res.reason
    assert len(net.requests) == 2      # 第二次搜索后超限，第三次没发出去


async def test_unpriced_backend_is_announced_when_a_cost_cap_is_set(ws):
    net = Net().on("POST", DS_SEARCH, ds_search())
    h = make_web_harness(ws, ["好"], net, budget__max_cost_usd=1.0, web__backend={"deepseek": {"price_per_call": 0}})
    res = await h.run("x")
    notes = [e.data for e in res.events if e.type == "notice" and e.data.get("code") == "unpriced_web"]
    assert notes and "deepseek" in notes[0]["text"]
    priced = make_web_harness(ws, ["好"], net, budget__max_cost_usd=1.0)
    assert not any(e.data.get("code") == "unpriced_web" for e in (await priced.run("x")).events if e.type == "notice")


def test_state_round_trip_and_child_propagation():
    b = Budget(BudgetConfig(max_cost_usd=1.0)).enable_web(5, 3)
    child = b.child(BudgetConfig(max_turns=3))
    child.charge_web("search", cost_usd=0.25)
    child.charge_web("fetch", cost_usd=0.25)
    child.charge_web("search", counted=False)
    assert (b.web_searches, b.web_fetches, b.tool_cost_usd) == (1, 1, 0.5)
    assert b.cost_usd == 0.5
    restored = Budget(BudgetConfig()).enable_web(5, 3).restore(b.state())
    assert (restored.web_searches, restored.web_fetches, restored.tool_cost_usd) == (1, 1, 0.5)
    assert b.child().limits.max_cost_usd == 0.5   # 子预算拿到的是扣掉联网花费之后的余额


def test_budget_without_web_keeps_its_old_shape():
    b = Budget(BudgetConfig())
    assert not any(k.startswith("web_") or k == "tool_cost_usd" for k in b.state())
    assert not any(k.startswith("web_") or k == "tool_cost_usd" for k in b.snapshot())
