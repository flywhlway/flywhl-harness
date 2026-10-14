"""03 · 取消语义：可中断工具立即停、不可中断工具等宽限期、流式输出保留已生成文本。

运行：python examples/03_cancel_semantics.py
"""

import asyncio

from _common import harness, say, temp_workspace, title

from flywhl_harness import ScriptedProvider


async def scenario(name: str, script, prompt: str, cancel_after: float, **cfg) -> None:
    title(name)
    h = harness(temp_workspace("03"), ScriptedProvider(script), **cfg)
    s = h.create_session(name)
    run = await h.start_run(s.id, prompt)
    await asyncio.sleep(cancel_after)
    t0 = asyncio.get_running_loop().time()
    await h.cancel(run.id, reason="用户点击停止", by="demo-user")
    rec = await h.wait(run.id)
    say(f"取消后 {asyncio.get_running_loop().time() - t0:.2f}s 结束，状态 {rec.status}；原因：{rec.status_reason}")
    for m in h.log(s.id).messages():
        if m.role == "tool":
            print(f"  tool {m.tool_name}: status={m.details.get('status')} {m.text[:70]!r}")
        elif m.role == "assistant" and m.stop_reason == "aborted":
            print(f"  assistant（aborted，保留已生成的 {len(m.text)} 字）：{m.text[:40]!r}…")
    trail = [(e.data["from"], e.data["to"]) for e in h.store.list_events(run.id) if e.type == "status"]
    print(f"  状态迁移：{trail}")
    await h.aclose()


async def main() -> None:
    await scenario("流式输出中途取消", [{"text": "这是一段很长很长的回答。" * 50, "delay": 0.01}], "写一篇长文", 0.3)
    await scenario("取消可中断工具（sleep）", [{"tool_calls": [{"name": "sleep", "args": {"seconds": 30}}]}], "等 30 秒", 0.3)

    title("取消不可中断工具（send_notification，宽限期内完成）")
    h = harness(temp_workspace("03"), ScriptedProvider([{"tool_calls": [{"name": "send_notification", "args": {"to": "ops", "message": "hi"}}]}]),
                cancel_grace_seconds=3)
    h.tools.get("send_notification").latency = 1.0
    s = h.create_session("x")
    run = await h.start_run(s.id, "发通知")
    await asyncio.sleep(0.3)
    await h.cancel(run.id, reason="用户点击停止", by="demo-user")
    rec = await h.wait(run.id)
    m = [m for m in h.log(s.id).messages() if m.role == "tool"][0]
    say(f"已发出的外部请求没有被半途打断：status={m.details['status']}，completed_after_cancel={m.details.get('completed_after_cancel')}；Run={rec.status}")
    await h.aclose()


if __name__ == "__main__":
    asyncio.run(main())
