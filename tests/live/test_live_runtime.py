"""真实模型 · 运行时：流式中途取消、插话、暂停/恢复、kill -9 崩溃接管、回放与轨迹、Web API 端到端。"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from flywhl_harness import Store
from flywhl_harness.events import E
from flywhl_harness.observe.replay import replay_run
from flywhl_harness.observe.trace import export_trajectory

from .conftest import live_harness, live_model, need, note, run, tools_used, transcript

pytestmark = pytest.mark.live


async def wait_event(h, run_id, pred, timeout=120.0):
    async with asyncio.timeout(timeout):
        async for ev in h.subscribe(run_id):
            if pred(ev):
                return ev
    raise AssertionError("没有等到期望的事件")


async def test_cancel_mid_stream_then_continue_session(ws):
    h = live_harness(ws, thinking="disabled")
    async with h:
        s = h.create_session("取消")
        r = await h.start_run(s.id, "写一篇 3000 字的文章，介绍 Agent Harness 的历史。")
        await wait_event(h, r.id, lambda e: e.type == E.MESSAGE_UPDATE and e.data.get("kind") == "text")
        t0 = time.monotonic()
        await h.cancel(r.id, reason="测试取消")
        rec = await asyncio.wait_for(h.wait(r.id), 30)
        elapsed = time.monotonic() - t0
        assert rec.status == "cancelled" and elapsed < 10, (rec.status, elapsed)   # 流式中途取消能及时断开连接
        aborted = [m for m in h.log(s.id).context().messages if m.role == "assistant"]
        assert aborted and aborted[-1].stop_reason == "aborted" and aborted[-1].text    # 已生成的文本保留并标记
        res = await run(h, "上一条被打断了，没关系。现在只回复：继续正常", session_id=s.id, label="取消后继续")
    assert res.status == "completed" and "继续正常" in res.text, transcript(h, s.id)   # 带着半截消息的历史仍合法


async def test_steer_mid_run_is_followed(ws):
    for i in range(1, 4):
        (ws / f"f{i}.txt").write_text(f"第 {i} 个文件\n", encoding="utf-8")
    h = live_harness(ws)
    async with h:
        s = h.create_session("插话")
        r = await h.start_run(s.id, "依次读取 f1.txt、f2.txt、f3.txt，每次只读一个文件，读完后总结。")
        await wait_event(h, r.id, lambda e: e.type == E.TOOL_END)
        assert h.steer(r.id, "补充要求：最终回复的第一行必须是 STEER-ACK")
        rec = await asyncio.wait_for(h.wait(r.id), 240)
    assert rec.status == "completed", transcript(h, s.id)
    assert "STEER-ACK" in (rec.result or {}).get("text", ""), transcript(h, s.id)
    kinds = [m.kind for m in h.log(s.id).context().messages if m.role == "user"]
    assert "steer" in kinds


async def test_pause_and_resume_at_safe_point(ws):
    (ws / "a.txt").write_text("暂停测试的数字是 66\n", encoding="utf-8")
    h = live_harness(ws)
    async with h:
        s = h.create_session("暂停")
        r = await h.start_run(s.id, "先用 sleep 工具等待 3 秒，然后读取 a.txt，告诉我里面的数字。")
        await wait_event(h, r.id, lambda e: e.type == E.TOOL_START and e.data["name"] == "sleep")
        assert await h.pause(r.id)
        rec = await asyncio.wait_for(h.wait(r.id), 60)
        assert rec.status == "paused", rec.status
        await h.resume(r.id)
        rec = await asyncio.wait_for(h.wait(r.id), 240)
    assert rec.status == "completed" and "66" in (rec.result or {}).get("text", ""), transcript(h, s.id)


async def test_kill_9_then_recover_completes(ws, tmp_path):
    """真进程崩溃：flywhl run 跑到一半被 kill -9，另一个进程 flywhl recover 接管并完成。"""
    need(live_model())
    (ws / "a.txt").write_text("崩溃恢复的数字是 314\n", encoding="utf-8")
    (ws / "flywhl.toml").write_text(f'model = "{live_model()}"\nheartbeat_seconds = 0.5\nstale_after_seconds = 3\n'
                                    'snapshots = false\n[permissions]\non_ask = "allow"\n', encoding="utf-8")
    exe = [sys.executable, "-m", "flywhl_harness.cli"]
    env = {**os.environ, "FLYWHL_FALLBACK_MODELS": ""}
    env.pop("LLM_FALLBACK_MODEL", None)
    proc = subprocess.Popen(exe + ["run", "--json", "-w", str(ws),
                                   "先用 sleep 工具等待 20 秒，然后读取 a.txt，只回复里面的数字。"],
                            cwd=ws, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    deadline = time.monotonic() + 120
    run_id = None
    for line in proc.stdout:            # 等到 sleep 工具开始执行，再杀掉进程
        ev = json.loads(line) if line.startswith("{") else {}
        run_id = run_id or ev.get("run_id")
        if ev.get("type") == E.TOOL_START and ev["data"]["name"] == "sleep":
            break
        assert time.monotonic() < deadline, "等不到 sleep 工具开始"
    proc.send_signal(signal.SIGKILL)
    proc.wait()
    await asyncio.sleep(4)              # 心跳过期，才算“Worker 已死”
    out = subprocess.run(exe + ["recover", "-w", str(ws)], cwd=ws, capture_output=True, text=True, env=env, timeout=240)
    store = Store(ws / ".flywhl" / "harness.db")
    rec = store.get_run(run_id)
    assert rec.status == "completed", (rec.status, out.stdout[-2000:], out.stderr[-2000:])
    assert "314" in (rec.result or {}).get("text", "")
    assert rec.attempt >= 1 or any(e.type == E.NOTICE and e.data.get("code") == "recover" for e in store.list_events(run_id))


async def test_replay_of_real_trajectory_is_deterministic(ws):
    (ws / "a.txt").write_text("回放的数字是 2718\n", encoding="utf-8")
    h = live_harness(ws)
    async with h:
        res = await run(h, "先列出目录，再读取 a.txt，告诉我数字。", label="录制真实轨迹")
        assert res.status == "completed", transcript(h, res.session_id)
        rep = await replay_run(h.store, res.run_id, config=h.config, base_tools=h.tools)
        traj = export_trajectory(h.store, res.run_id)
    assert rep.status == "completed" and rep.divergence is None, rep.notes   # 不调用模型，按真实轨迹确定性重放
    assert rep.same_tool_sequence and rep.replay_tools == tools_used(res)
    assert len(traj["fingerprints"]) == len([m for m in traj["messages"] if m["role"] == "assistant"])


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_web_api_end_to_end_with_approval(ws):
    """前后端链路：REST 建会话、启动 Run → SSE 实时事件 → 审批接口批准 → 会话详情里能看到完整结果。"""
    import uvicorn

    from flywhl_harness.server.app import create_app

    h = live_harness(ws, permissions__on_ask="wait")
    app = create_app(h)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    base = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient(base_url=base, timeout=30) as c:
            for _ in range(50):
                try:
                    if (await c.get("/api/info")).status_code == 200:
                        break
                except httpx.TransportError:
                    await asyncio.sleep(0.1)
            info = (await c.get("/api/info")).json()
            assert info["model"] and info["provider"] != "mock"
            sid = (await c.post("/api/sessions", json={"title": "web e2e"})).json()["id"]
            run_id = (await c.post(f"/api/sessions/{sid}/runs", json={"prompt": "创建文件 web.txt，内容是 hello web。"})).json()["id"]
            types: list[str] = []
            approved = False
            async with asyncio.timeout(240):
                async with c.stream("GET", f"/api/runs/{run_id}/events", timeout=None) as resp:
                    event = None
                    async for line in resp.aiter_lines():
                        if line.startswith("event: "):
                            event = line[7:]
                            types.append(event)
                        if event == E.APPROVAL_REQUEST and line.startswith("data: ") and not approved:
                            pend = (await c.get("/api/approvals")).json()
                            assert pend and pend[0]["tool"] == "write" and pend[0].get("preview")   # 审批卡片能拿到 diff 预览
                            assert (await c.post(f"/api/approvals/{pend[0]['id']}", json={"approved": True})).status_code == 200
                            approved = True
                        if event == "stream_end":
                            break
            detail = (await c.get(f"/api/sessions/{sid}")).json()
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 15)   # lifespan 退出时会关闭 harness
    assert approved and (ws / "web.txt").read_text().strip().startswith("hello web")
    assert {E.RUN_START, E.MESSAGE_UPDATE, E.TOOL_START, E.TOOL_END, E.RUN_END} <= set(types)
    assert detail["runs"][0]["status"] == "completed"
    assert any(m["role"] == "assistant" for m in detail["messages"])
    u = detail["runs"][0]["usage"]
    note(type("R", (), {"status": "completed", "usage": u})(), "Web API 端到端")
