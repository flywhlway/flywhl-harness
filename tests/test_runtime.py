"""重试与降级、DAG 调度、端云指令下发、子代理、SSE 断线续传、MCP、回放、评测统计、schema。"""

from __future__ import annotations

import asyncio
import json
import random
import sys
import time
from pathlib import Path
from typing import Annotated, Literal

import httpx
import pytest

from flywhl_harness import ScriptedProvider
from flywhl_harness.cancel import CancelToken
from flywhl_harness.events import E
from flywhl_harness.llm.base import ErrorKind, ModelRequest, Provider, ProviderError
from flywhl_harness.llm.resilient import ResilientProvider
from flywhl_harness.observe.evals import mcnemar_exact, wilson_interval
from flywhl_harness.runtime.dispatch import CommandDispatcher, EdgeExecutor, FlakyTransport
from flywhl_harness.runtime.retry import CircuitBreaker, RetryBudget, RetryPolicy, retry_async
from flywhl_harness.runtime.scheduler import CycleError, DagScheduler, TaskSpec, UpstreamFailed, critical_path
from flywhl_harness.tools.schema import schema_from_signature, validate

from .conftest import make_harness, tool_ends

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------- 重试
def test_full_jitter_bounds_and_retry_after():
    p = RetryPolicy(base_delay=1, max_delay=8)
    rng = random.Random(1)
    for attempt in range(1, 8):
        d = p.backoff(attempt, rng=rng)
        assert 0 <= d <= min(8, 2 ** (attempt - 1))
    assert p.backoff(1, retry_after=5, rng=rng) >= 5


def test_circuit_breaker_states():
    t = [0.0]
    b = CircuitBreaker(failure_threshold=2, reset_timeout=10, clock=lambda: t[0])
    b.failure(); b.failure()
    assert b.state == "open" and not b.allow()
    t[0] = 11
    assert b.state == "half_open" and b.allow() and not b.allow()  # 半开只放一个探测
    b.success()
    assert b.state == "closed"


def test_retry_budget():
    t = [0.0]
    rb = RetryBudget(ratio=0.1, min_retries=1, clock=lambda: t[0])
    for _ in range(10):
        rb.record_request()
    assert rb.can_retry()
    rb.record_retry()
    assert not rb.can_retry()  # 10 个请求、10% 比例 → 只允许 1 次重试


async def test_retry_async_respects_classification():
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise ProviderError(ErrorKind.RATE_LIMIT, "429")
        return "ok"

    out = await retry_async(flaky, RetryPolicy(max_attempts=5, base_delay=0.001), is_retryable=lambda e: e.retryable)
    assert out == "ok" and len(calls) == 3

    async def bad():
        raise ProviderError(ErrorKind.AUTH, "401")

    with pytest.raises(ProviderError):
        await retry_async(bad, RetryPolicy(max_attempts=5, base_delay=0.001), is_retryable=lambda e: e.retryable)


async def test_resilient_provider_retry_then_fallback():
    primary = ScriptedProvider([ProviderError(ErrorKind.OVERLOADED, "529")] * 10, mode="sequential")
    backup = ScriptedProvider(["来自备用模型"], mode="sequential")
    rp = ResilientProvider([(primary, "m1"), (backup, "m2")], RetryPolicy(max_attempts=2, base_delay=0.001))
    evs = [ev async for ev in rp.stream(ModelRequest(model="m1", system="", messages=[]), CancelToken())]
    kinds = [e.type for e in evs]
    assert "retry" in kinds and "notice" in kinds
    assert evs[-1].message.text == "来自备用模型"


async def test_resilient_provider_does_not_retry_auth():
    p = ScriptedProvider([ProviderError(ErrorKind.AUTH, "401")], mode="sequential")
    rp = ResilientProvider([(p, "m")], RetryPolicy(max_attempts=5, base_delay=0.001))
    with pytest.raises(ProviderError):
        async for _ in rp.stream(ModelRequest(model="m", system="", messages=[]), CancelToken()):
            pass
    assert p.calls == 1


async def test_loop_retries_model_and_reports(ws):
    prov = ScriptedProvider([ProviderError(ErrorKind.RATE_LIMIT, "429", retry_after=0.01), "成功"], mode="sequential")
    h = make_harness(ws, ResilientProvider([(prov, "scripted")], RetryPolicy(max_attempts=3, base_delay=0.001)))
    res = await h.run("hi")
    assert res.status == "completed" and any(e.type == E.MODEL_RETRY for e in res.events)


async def test_context_overflow_forces_compaction(ws):
    from flywhl_harness.types import user_message

    prov = ScriptedProvider([ProviderError(ErrorKind.CONTEXT_OVERFLOW, "prompt is too long"), "压缩后成功"], mode="sequential")
    h = make_harness(ws, prov, context__keep_recent_tokens=50)
    s = h.create_session("t")
    for i in range(6):
        h.log(s.id).append_message(user_message("历史" * 300))
    res = await h.run("继续", session_id=s.id)
    assert res.status == "completed"
    assert any(e.type == E.COMPACTION_END and not e.data.get("skipped") for e in res.events)


# ---------------------------------------------------------------------- DAG
async def test_dag_order_concurrency_and_policies():
    running, peak, order = [0], [0], []

    def job(name, fail=False, delay=0.02):
        async def fn(up):
            running[0] += 1
            peak[0] = max(peak[0], running[0])
            await asyncio.sleep(delay)
            running[0] -= 1
            order.append(name)
            if fail:
                raise RuntimeError(f"{name} 失败")
            return {"name": name, "up": sorted(up)}
        return fn

    tasks = [TaskSpec("a", job("a")), TaskSpec("b", job("b")), TaskSpec("c", job("c")),
             TaskSpec("d", job("d"), deps=["a", "b"]), TaskSpec("e", job("e", fail=True), deps=["c"]),
             TaskSpec("f", job("f"), deps=["e"]), TaskSpec("g", job("g"), deps=["d"])]
    res = await DagScheduler(max_concurrency=2, per_tenant=2, failure_policy="skip_downstream").run(tasks)
    assert peak[0] <= 2
    assert res["d"].value["up"] == ["a", "b"] and order.index("d") > max(order.index("a"), order.index("b"))
    assert res["e"].status == "failed" and res["f"].status == "skipped" and res["g"].status == "ok"
    path, total = critical_path(tasks, res)
    assert path[-1] in ("g", "e") and total > 0

    order.clear()
    res2 = await DagScheduler(max_concurrency=4, failure_policy="fail_fast").run(
        [TaskSpec("x", job("x", fail=True)), TaskSpec("y", job("y", delay=0.5)), TaskSpec("z", job("z"), deps=["y"])])
    assert res2["x"].status == "failed" and res2["z"].status in ("cancelled", "skipped")


async def test_dag_retries_and_cycle():
    n = [0]

    async def flaky(up):
        n[0] += 1
        if n[0] < 3:
            raise RuntimeError("瞬时故障")
        return "ok"

    res = await DagScheduler().run([TaskSpec("t", flaky, retries=3)])
    assert res["t"].status == "ok" and res["t"].attempts == 3
    with pytest.raises(CycleError):
        await DagScheduler().run([TaskSpec("a", flaky, deps=["b"]), TaskSpec("b", flaky, deps=["a"])])


# ---------------------------------------------------------------------- 端云
async def test_dispatch_exactly_once_effect_over_flaky_network():
    tr = FlakyTransport(drop=0.35, dup=0.3, seed=42)
    t = [0.0]
    cloud = CommandDispatcher(tr, ack_timeout=1.0, max_attempts=50, clock=lambda: t[0])
    executed: list[str] = []

    async def handler(payload):
        executed.append(payload["n"])
        return f"done {payload['n']}"

    edge = EdgeExecutor("dev1", tr, handler)
    tr.device, tr.cloud = edge, cloud
    cmds = [cloud.submit("dev1", {"n": i}, idem_key=f"k{i}") for i in range(20)]
    assert cloud.submit("dev1", {"n": 0}, idem_key="k0") is cmds[0]  # 重复提交：同一条指令
    await cloud.pump()  # 设备离线：什么都不发
    assert tr.stats["sent"] == 0
    cloud.set_online("dev1", True)
    for _ in range(60):
        await cloud.pump()
        await tr.drain()
        t[0] += 1.1
        if all(c.status == "acked" for c in cmds):
            break
    assert all(c.status == "acked" for c in cmds)
    assert sorted(executed) == list(range(20)), "丢包、重复、乱序之下，每条指令恰好执行一次"
    assert tr.stats["dropped"] > 0 and tr.stats["duplicated"] > 0


async def test_dispatch_ttl_expire():
    tr = FlakyTransport(drop=1.0)
    t = [0.0]
    cloud = CommandDispatcher(tr, clock=lambda: t[0])
    c = cloud.submit("dev", {}, idem_key="k", ttl=5)
    t[0] = 10
    await cloud.pump()
    assert c.status == "expired"
    assert not cloud.on_ack({"id": c.id, "ok": True}), "过期后的迟到 ACK 被忽略"


# ---------------------------------------------------------------------- 子代理
class RoleRouter(Provider):
    """按请求角色（主代理 / 子代理）分发到不同脚本。"""
    name = "mock"

    def __init__(self, main, sub):
        self.main, self.sub = ScriptedProvider(main), ScriptedProvider(sub)

    async def stream(self, req, cancel):
        src = self.sub if req.metadata.get("role") == "subagent" else self.main
        async for ev in src.stream(req, cancel):
            yield ev


async def test_subagent_contract(ws):
    (ws / "main.py").write_text("print('hi')\n")
    contract = {"status": "ok", "summary": "入口是 main.py", "findings": [{"claim": "main.py 打印 hi", "evidence": ["main.py:1"]}],
                "artifacts": ["main.py"]}
    prov = RoleRouter(
        main=[{"tool_calls": [{"name": "task", "args": {"description": "找入口", "prompt": "找到入口文件"}}]}, "子代理说入口是 main.py"],
        sub=[{"tool_calls": [{"name": "ls", "args": {}}]}, "这是我的结论（违约：不是 JSON）",
             "```json\n" + json.dumps(contract, ensure_ascii=False) + "\n```"],
    )
    h = make_harness(ws, prov)
    res = await h.run("委托子任务")
    assert res.status == "completed"
    end = tool_ends(res.events)[0]
    assert end["details"]["contract"]["status"] == "ok"
    assert len(end["details"]["child_run_ids"]) == 2, "第一次违约后要求修正一次"
    # 父上下文里只有契约，没有子代理的中间过程
    parent_msgs = h.log(res.session_id).messages()
    assert not any(m.tool_name == "ls" for m in parent_msgs)
    assert any(e.type == E.TOOL_UPDATE and "subagent" in e.data["update"] for e in res.events)


# ---------------------------------------------------------------------- SSE 断线续传
async def test_sse_resume_with_last_event_id(ws):
    from flywhl_harness.server.app import create_app

    h = make_harness(ws, [{"tool_calls": [{"name": "ls", "args": {}}]}, "完成"])
    app = create_app(h, auto_recover=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as c:
        s = (await c.post("/api/sessions", json={"title": "t"})).json()
        run = (await c.post(f"/api/sessions/{s['id']}/runs", json={"prompt": "hi"})).json()
        await h.wait(run["id"])
        full = (await c.get(f"/api/runs/{run['id']}/events")).text
        ids = [int(l[4:]) for l in full.splitlines() if l.startswith("id: ")]
        assert ids == sorted(ids) and ids[0] == 1
        mid = ids[len(ids) // 2]
        tail = (await c.get(f"/api/runs/{run['id']}/events", headers={"Last-Event-ID": str(mid)})).text
        tail_ids = [int(l[4:]) for l in tail.splitlines() if l.startswith("id: ")]
        assert tail_ids == [i for i in ids if i > mid], "只补发 Last-Event-ID 之后的事件，不重不漏"
        ctx = (await c.get(f"/api/sessions/{s['id']}/context")).json()
        assert ctx["stats"]["window"] > 0 and ctx["items"]


# ---------------------------------------------------------------------- MCP
async def test_mcp_tools(ws):
    h = make_harness(ws, [
        {"tool_calls": [{"name": "mcp__demo__echo", "args": {"text": "你好 MCP"}}]},
        "完成",
    ], mcp_servers={"demo": {"command": sys.executable, "args": [str(ROOT / "examples" / "mcp_echo_server.py")]}})
    async with h:
        assert "mcp__demo__add_note" in h.tools
        assert str(h.tools.get("mcp__demo__echo").effect) == "read"
        res = await h.run("调用 MCP")
    assert tool_ends(res.events)[0]["content"] == "你好 MCP"


# ---------------------------------------------------------------------- 回放
async def test_deterministic_replay(ws):
    from flywhl_harness.observe.replay import replay_run

    (ws / "a.txt").write_text("1\n")
    h = make_harness(ws, [
        {"tool_calls": [{"name": "read", "args": {"path": "a.txt"}}]},
        {"tool_calls": [{"name": "send_notification", "args": {"to": "x", "message": "y"}}]},
        "完成",
    ])
    res = await h.run("读然后通知")
    from flywhl_harness.tools.demo import FakeNotifyService
    svc = FakeNotifyService(h.data_dir / "external_notify.db")
    before = svc.count()
    rep = await replay_run(h.store, res.run_id, config=h.config, base_tools=h.tools)
    assert rep.status == "completed" and rep.same_tool_sequence and rep.same_final_text
    assert rep.divergence is None
    assert svc.count() == before, "回放不得重放副作用"


# ---------------------------------------------------------------------- 统计与 schema
def test_wilson_and_mcnemar():
    lo, hi = wilson_interval(8, 10)
    assert 0.45 < lo < 0.5 and 0.94 < hi < 0.97
    assert wilson_interval(0, 0) == (0.0, 1.0)
    a = [True] * 10 + [False] * 10
    b = [True] * 10 + [True] * 3 + [False] * 7
    r = mcnemar_exact(a, b)
    assert r["b_only"] == 3 and r["a_only"] == 0 and r["p_value"] == 0.25  # 3:0 也不显著


def test_schema_validation_and_coercion():
    async def f(path: Annotated[str, "路径"], limit: int = 10, mode: Literal["a", "b"] = "a", tags: list[str] | None = None):
        ...

    sch = schema_from_signature(f)
    assert sch["required"] == ["path"] and sch["properties"]["mode"]["enum"] == ["a", "b"]
    v, errs = validate(sch, {"path": "x", "limit": "5"})
    assert not errs and v["limit"] == 5 and v["mode"] == "a"
    _, errs = validate(sch, {"limit": 1.5, "mode": "c", "extra": 1})
    joined = "\n".join(errs)
    assert "缺少必填参数" in joined and "不在允许范围" in joined and "未知参数" in joined


async def test_bash_own_timeout_keeps_partial_output(ws):
    """回归：bash 自己的超时先于执行器兜底计时器触发，保留部分输出与“已终止进程组”提示。"""
    h = make_harness(ws, [{"tool_calls": [{"name": "bash", "args": {"command": "echo 开始; sleep 30", "timeout": 1}}]}, "好"],
                     permissions__mode="bypass")
    res = await h.run("跑个慢命令")
    end = tool_ends(res.events)[0]
    assert end["status"] == "error" and "开始" in end["content"] and "已终止进程组" in end["content"]


async def test_edit_refuses_non_utf8(ws):
    (ws / "gbk.txt").write_bytes("中文内容".encode("gbk"))
    h = make_harness(ws, [{"tool_calls": [{"name": "edit", "args": {"path": "gbk.txt", "old_string": "a", "new_string": "b"}}]}, "好"],
                     permissions__mode="bypass")
    res = await h.run("改一下")
    assert "不是 UTF-8" in tool_ends(res.events)[0]["content"]
    assert (ws / "gbk.txt").read_bytes() == "中文内容".encode("gbk"), "文件不能被改坏"


def test_json_string_array_coercion():
    v, errs = validate({"type": "array", "items": {"type": "string"}}, '["a", "b"]')
    assert not errs and v == ["a", "b"]
    v, errs = validate({"type": "array", "items": {"type": "string"}}, "单个值")
    assert not errs and v == ["单个值"]


async def test_dag_tenant_limit_does_not_block_other_tenants():
    """回归：等待租户名额的任务不能占着全局名额——否则别的租户被饿死（队头阻塞）。"""
    t0 = time.monotonic()
    started: dict[str, float] = {}

    def job(name, delay):
        async def fn(up):
            started[name] = time.monotonic() - t0
            await asyncio.sleep(delay)
            return name
        return fn

    tasks = [TaskSpec("a1", job("a1", 0.3), tenant="A"), TaskSpec("a2", job("a2", 0.3), tenant="A"),
             TaskSpec("b1", job("b1", 0.01), tenant="B")]
    await DagScheduler(max_concurrency=2, per_tenant=1).run(tasks)
    assert started["b1"] < 0.15, started


async def test_dag_continue_policy_passes_failure_and_cancel_is_immediate():
    async def boom(up):
        raise RuntimeError("坏了")

    async def downstream(up):
        return f"上游状态：{up['bad'].status}" if isinstance(up["bad"], UpstreamFailed) else "?"

    res = await DagScheduler(failure_policy="continue").run([TaskSpec("bad", boom), TaskSpec("next", downstream, deps=["bad"])])
    assert res["next"].value == "上游状态：failed"

    token = CancelToken()

    async def slow(up):
        await asyncio.sleep(5)

    asyncio.get_running_loop().call_later(0.05, token.cancel, "用户取消")
    t0 = time.monotonic()
    res = await DagScheduler(cancel=token).run([TaskSpec("s", slow), TaskSpec("t", slow, deps=["s"])])
    assert time.monotonic() - t0 < 1.0, "取消应当立即生效，而不是等任务自然结束"
    assert res["s"].status == "cancelled" and res["s"].ended_at is not None and res["t"].status == "cancelled"


async def test_edge_dedupes_duplicate_arriving_mid_execution():
    """执行过程中到达的重复指令：等同一次执行的结果，而不是再执行一遍。"""
    calls = []

    async def handler(payload):
        calls.append(payload)
        await asyncio.sleep(0.05)
        return "done"

    class Sink(FlakyTransport):
        def __init__(self):
            super().__init__(drop=0, dup=0)
            self.acks = []

        async def to_cloud(self, msg):
            self.acks.append(msg)

    tr = Sink()
    edge = EdgeExecutor("d1", tr, handler)
    msg = {"type": "command", "id": "c1", "payload": {"op": "x"}}
    await asyncio.gather(edge.on_message(msg), edge.on_message(msg))
    assert len(calls) == 1 and edge.side_effects == 1 and len(tr.acks) == 2


def test_circuit_breaker_probe_is_released_or_expires():
    t = [0.0]
    br = CircuitBreaker(failure_threshold=1, reset_timeout=10, clock=lambda: t[0])
    br.failure()
    t[0] = 11
    assert br.allow() and not br.allow(), "半开状态只放行一个探测"
    br.release()  # 探测被取消：归还名额
    assert br.allow()
    t[0] = 30  # 探测方再也没回报：名额过期后允许新的探测
    assert br.allow()


def test_fake_notify_rejects_same_key_different_payload(tmp_path):
    from flywhl_harness.tools.demo import FakeNotifyService, IdempotencyConflict

    svc = FakeNotifyService(tmp_path / "n.db")
    assert svc.send("k1", "ops", "部署完成")[0] is True
    assert svc.send("k1", "ops", "部署完成")[0] is False
    with pytest.raises(IdempotencyConflict):
        svc.send("k1", "ops", "完全不同的内容")


def test_fingerprint_sees_tool_description_change():
    from flywhl_harness.llm.replay import context_fingerprint

    tool = {"name": "read", "description": "读文件", "parameters": {"type": "object"}}
    a = context_fingerprint(ModelRequest(model="m", system="s", messages=[], tools=[tool]))
    b = context_fingerprint(ModelRequest(model="m", system="s", messages=[], tools=[{**tool, "description": "读文件（新版）"}]))
    assert a != b


def test_dispatcher_rejects_reused_key_with_different_payload():
    from flywhl_harness.runtime.dispatch import IdempotencyKeyReused

    d = CommandDispatcher(FlakyTransport(drop=0, dup=0))
    a = d.submit("dev1", {"op": "lock"}, "k1")
    assert d.submit("dev1", {"op": "lock"}, "k1") is a
    with pytest.raises(IdempotencyKeyReused):
        d.submit("dev1", {"op": "unlock"}, "k1")


async def test_cancelling_only_the_child_returns_failed_contract(ws):
    """单独取消子 Run：父代理收到一份 failed 回传并继续，而不是一条看不懂的 RunCancelled 异常。"""
    script = [
        {"tool_calls": [{"name": "task", "args": {"description": "慢查", "prompt": "等一会儿"}}]},
        {"tool_calls": [{"name": "sleep", "args": {"seconds": 5}}]},   # 子代理
        "父代理：子任务被取消，我直接回答。",
    ]
    h = make_harness(ws, ScriptedProvider(script, mode="sequential"))
    s = h.create_session("t")
    run = await h.start_run(s.id, "委托")
    child = None
    for _ in range(100):
        child = next((r for r in h.store.child_runs(run.id) if r.status == "running"), None)
        if child:
            break
        await asyncio.sleep(0.02)
    assert child is not None
    await asyncio.sleep(0.1)
    assert await h.cancel(child.id, reason="不需要了")
    rec = await h.wait(run.id)
    end = next(e for e in tool_ends(h.store.list_events(run.id)) if e["name"] == "task")
    assert rec.status == "completed" and "子代理被取消" in end["content"]
    assert h.store.get_run(child.id).status == "cancelled"


async def test_console_runs_and_approval_preview_endpoints(ws):
    """控制台用到的两个接口：跨会话运行列表（带会话标题），审批列表（带按当前工作区现算的 diff 预览）。"""
    from flywhl_harness.runtime.store import ApprovalRecord
    from flywhl_harness.server.app import create_app

    (ws / "app.py").write_text("x = 1\n")
    h = make_harness(ws, ["好"])
    app = create_app(h, auto_recover=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765") as c:
        s = (await c.post("/api/sessions", json={"title": "控制台测试"})).json()
        run = (await c.post(f"/api/sessions/{s['id']}/runs", json={"prompt": "hi"})).json()
        await h.wait(run["id"])
        runs = (await c.get("/api/runs")).json()
        assert runs[0]["id"] == run["id"] and runs[0]["session_title"] == "控制台测试" and "checkpoint" not in runs[0]
        h.store.create_approval(ApprovalRecord(id="ap_t", run_id=run["id"], session_id=s["id"], call_id="c1", tool="edit",
                                               args={"path": "app.py", "old_string": "x = 1", "new_string": "x = 2"},
                                               summary="编辑 app.py", risk="medium", reason="写操作需要确认", status="pending"))
        ap = next(a for a in (await c.get("/api/approvals")).json() if a["id"] == "ap_t")
        assert "-x = 1" in ap["preview"] and "+x = 2" in ap["preview"] and ap["session_title"] == "控制台测试"
