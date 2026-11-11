"""会话能力位：建会话时 clamp 并钉工具名单；开工后只能收紧（mask，不改工具名单）；子代理取整条链上最严的值；
没启用联网时一切与以前相同。"""

from __future__ import annotations

import pytest

from flywhl_harness import HarnessConfig, ScriptedProvider, Store
from flywhl_harness.llm.base import Provider
from flywhl_harness.runtime.capabilities import clamp, current_web, session_web, strictest

from .conftest import make_harness
from .webkit import DS_FETCH, DS_SEARCH, Net, ds_search, fetch_call, fixture, make_web_harness, search_call, tool_results

BASE_META = {"system_prompt", "model", "provider", "tools", "project_context", "mode", "grants", "clear_watermark"}


def web_net() -> Net:
    import httpx

    return Net().on("POST", DS_SEARCH, ds_search()).on("POST", DS_FETCH, httpx.Response(200, json=fixture("deepseek", "fetch")))


def test_clamp_and_strictest():
    assert clamp("fetch", "search", "读取不可用") == ("search", "读取不可用")
    assert clamp("search", "fetch") == ("search", "")
    assert clamp("bogus", "fetch") == ("off", "")
    assert clamp(None, "fetch") == ("off", "")
    assert strictest("fetch", "off", "search") == "off" and strictest() == "off"
    assert session_web({}) == "off" and session_web({"capabilities": {"web": "nope"}}) == "off"


async def test_disabled_web_changes_nothing(ws):
    h = make_harness(ws, [{"tool_calls": [{"name": "ls", "args": {}}]}, "好"])
    s = h.create_session("x")
    assert set(s.meta) == BASE_META
    assert not any(n.startswith("web_") for n in s.meta["tools"])
    assert h.web is None and h.web_capability() is None
    res = await h.run("看看目录", session_id=s.id)
    req = h.provider.requests[0]
    assert not any(t["name"].startswith("web_") for t in req.tools)
    assert "联网" not in req.system and "web_search" not in req.system
    run = h.store.get_run(res.run_id)
    assert not any(k.startswith("web_") or k == "tool_cost_usd" for k in run.result)
    assert not any(k.startswith("web_") for k in run.checkpoint["budget"])
    usage_events = [e.data for e in res.events if e.type == "usage"]
    assert usage_events and not any("web_searches" in u for u in usage_events)
    assert "capabilities" not in h.store.get_session(s.id).meta


async def test_enabled_but_off_session_sends_the_same_request_as_disabled(ws):
    off = make_harness(ws, ["好"])
    await off.run("你好")
    on = make_web_harness(ws, ["好"], web_net(), web__default="off")
    await on.run("你好")
    a, b = off.provider.requests[0], on.provider.requests[0]
    assert a.system == b.system and a.tools == b.tools


@pytest.mark.parametrize("default,pinned", [("off", []), ("search", ["web_search"]), ("fetch", ["web_search", "web_fetch"])])
def test_tools_are_pinned_by_capability(ws, default, pinned):
    h = make_web_harness(ws, [], web_net(), web__default=default)
    s = h.create_session("x")
    assert [n for n in s.meta["tools"] if n.startswith("web_")] == pinned
    assert s.meta["capabilities"] == {"web": default, "requested": default, "reason": ""}


def test_requested_level_is_clamped_to_what_is_available(ws, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    h = make_web_harness(ws, [], web_net(), env={})   # 没有密钥
    assert h.web_ceiling()[0] == "off" and "缺少密钥" in h.web_ceiling()[1]
    cap = h.web_capability("fetch")
    assert cap["web"] == "off" and cap["requested"] == "fetch" and "缺少密钥" in cap["reason"]
    assert not any(n.startswith("web_") for n in h.create_session("x", capabilities=cap).meta["tools"])
    # 用户配置关掉的，请求什么都是 off；返回 None 表示会话元数据与以前一样
    assert make_web_harness(ws, [], web_net(), web__enabled=False).web_capability("fetch") is None


async def test_tightening_masks_the_tool_and_loosening_is_refused(ws):
    net = web_net()
    script = [search_call(), "第一轮好", fetch_call("https://pypi.org/project/httpx/"), "第二轮", search_call("b"), "第三轮"]
    h = make_web_harness(ws, script, net, web__default="fetch", web__fetch_scope="public")
    s = h.create_session("x")
    pinned = list(s.meta["tools"])
    first = await h.run("查", session_id=s.id)
    assert not tool_results(first.events, "web_search")[0]["is_error"]

    h.set_capabilities(s.id, "search")
    second = await h.run("读", session_id=s.id)
    denied = tool_results(second.events, "web_fetch")[0]
    assert denied["is_error"] and denied["status"] == "denied" and "只允许联网搜索" in denied["content"]
    assert "新建会话" in denied["content"]
    assert net.bodies(DS_FETCH) == []
    assert h.store.get_session(s.id).meta["tools"] == pinned          # 工具名单不变：前缀缓存不失效

    h.set_capabilities(s.id, "off")
    third = await h.run("再查", session_id=s.id)
    assert "已关闭联网" in tool_results(third.events, "web_search")[0]["content"]
    assert len(net.bodies(DS_SEARCH)) == 1

    with pytest.raises(PermissionError, match="只能收紧"):
        h.set_capabilities(s.id, "search")
    assert h.store.get_session(s.id).meta["capabilities"]["web"] == "off"
    assert h.store.get_session(s.id).meta["capabilities"]["reason"] == "开工后收紧为 off"


async def test_tightening_mid_run_applies_to_the_next_call(ws):
    net = web_net()
    holder: dict = {}

    def tighten_then_search(req):
        holder["h"].set_capabilities(holder["sid"], "off")
        return search_call("b")

    h = make_web_harness(ws, [search_call("a"), tighten_then_search, "好"], net)
    holder["h"] = h
    holder["sid"] = h.create_session("x").id
    res = await h.run("x", session_id=holder["sid"])
    a, b = tool_results(res.events, "web_search")
    assert not a["is_error"] and b["status"] == "denied"
    assert len(net.requests) == 1


def test_capability_survives_a_new_harness_on_the_same_store(ws, tmp_path):
    store = Store(tmp_path / "h.db")
    h = make_web_harness(ws, [], web_net(), store=store, web__default="fetch")
    sid = h.create_session("x").id
    h.set_capabilities(sid, "search")
    again = make_web_harness(ws, [], web_net(), store=Store(tmp_path / "h.db"), web__default="fetch")
    assert current_web(again.store, sid) == "search"


def test_chain_takes_the_strictest_level(ws):
    store = Store(":memory:")
    parent = store.create_session("p", str(ws), meta={"capabilities": {"web": "fetch"}})
    child = store.create_session("c", str(ws), parent_session_id=parent.id, meta={"capabilities": {"web": "fetch"}})
    assert current_web(store, child.id) == "fetch"
    store.update_session(parent.id, meta={"capabilities": {"web": "off"}})
    assert current_web(store, child.id) == "off"          # 父会话收紧，子代理下一次调用就受约束
    legacy = store.create_session("old", str(ws))
    assert current_web(store, legacy.id) == "off"         # 老会话没有能力位
    assert current_web(store, "s_missing") == "off"


class RoleRouter(Provider):
    name = "mock"

    def __init__(self, main, sub):
        self.main, self.sub = ScriptedProvider(main), ScriptedProvider(sub)

    async def stream(self, req, cancel):
        src = self.sub if req.metadata.get("role") == "subagent" else self.main
        async for ev in src.stream(req, cancel):
            yield ev


async def test_subagent_inherits_and_shares_the_run_limit(ws):
    net = web_net()
    task = {"tool_calls": [{"name": "task", "args": {"description": "查版本", "prompt": "查 httpx 最新版本"}}]}
    prov = RoleRouter(main=[search_call("a"), task, "好"], sub=[search_call("b"), search_call("c"), '{"status": "ok", "summary": "s"}'])
    h = make_web_harness(ws, prov, net, web__max_searches_per_run=2)
    res = await h.run("x")
    assert res.status == "completed"
    child = next(s for s in h.store.list_sessions(limit=10, include_children=True) if s.parent_session_id == res.session_id)
    assert child.meta["capabilities"] == {"web": "search", "requested": "search", "reason": "继承父会话"}
    child_runs = [r for r in h.store.list_runs(child.id, limit=10)]
    ends = [e for r in child_runs for e in tool_results(h.store.list_events(r.id), "web_search")]
    assert [e["is_error"] for e in ends] == [False, True]       # 父代理已经用掉一次：子代理只剩一次
    assert len(net.requests) == 2
    assert h.store.get_run(res.run_id).result["web_searches"] == 2


def test_inflight_reservations_are_shared_across_concurrent_subagents(ws):
    """并发的子代理各有自己的 Run 和 state，调用编号还可能相同：在途占位挂在顶层预算上、按 Run 区分，
    第二个子代理看得到第一个还没记账的调用，合计不超过本次运行的上限。"""
    from types import SimpleNamespace

    from flywhl_harness.agent.budget import Budget
    from flywhl_harness.config import BudgetConfig
    from flywhl_harness.tools.base import ToolContext

    tool = make_web_harness(ws, [], web_net(), web__max_searches_per_run=1).tools.get("web_search")
    top = Budget(BudgetConfig()).enable_web(1, 1)

    def sub(run_id: str) -> ToolContext:
        return ToolContext(run_id=run_id, session_id="s", call_id="c1", workspace=None, cancel=None, blobs=None,
                           idempotency_key=f"{run_id}:c1", run=SimpleNamespace(budget=top.child()))

    a, b = sub("r_a"), sub("r_b")
    assert tool._reserve(a) is None
    assert "搜索上限 1 次" in (tool._reserve(b) or "")
    tool._release(a)                      # 第一个失败退回占位后，第二个才能用
    assert tool._reserve(b) is None and top.web_inflight == {"r_b:c1": "search"}


async def test_subagent_of_a_tightened_parent_gets_the_tightened_level(ws):
    prov = RoleRouter(main=[{"tool_calls": [{"name": "task", "args": {"description": "d", "prompt": "p"}}]}, "好"],
                      sub=['{"status": "ok", "summary": "s"}'])
    h = make_web_harness(ws, prov, web_net(), web__default="fetch")
    sid = h.create_session("x").id
    h.set_capabilities(sid, "search")
    await h.run("x", session_id=sid)
    child = next(s for s in h.store.list_sessions(limit=10, include_children=True) if s.parent_session_id == sid)
    assert child.meta["capabilities"]["web"] == "search"
    assert "web_fetch" not in child.meta["tools"]


def test_native_search_in_extra_body_taints_new_sessions(ws):
    from flywhl_harness.config import native_web_search

    assert native_web_search({"enable_search": True, "search_options": {}}) == ["enable_search"]
    assert native_web_search({"tools": [{"type": "web_search_20250305"}]}) == ["tools[].type=web_search"]
    assert native_web_search({"enable_search": False}) == [] and native_web_search(None) == []
    h = make_harness(ws, [], extra_body={"enable_search": True})
    assert h.native_search == ["enable_search"]
    assert h.create_session("x").meta.get("tainted") is True
    assert make_harness(ws, []).create_session("y").meta.get("tainted") is None


async def test_native_search_notice(ws):
    h = make_harness(ws, ["好"], extra_body={"enable_search": True})
    res = await h.run("x")
    assert any(e.type == "notice" and e.data.get("code") == "native_web_search" for e in res.events)


def test_config_defaults_keep_web_off():
    cfg = HarnessConfig()
    assert cfg.web.enabled is False and cfg.web.default == "off" and cfg.web.fetch_scope == "ask"
