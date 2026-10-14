"""04 · 上下文工程：大结果外置为句柄 → 旧结果清理 → 摘要压缩，观察水位变化。

运行：python examples/04_context_compaction.py
"""

import asyncio

from _common import harness, render, say, temp_workspace, title

from flywhl_harness import ScriptedProvider
from flywhl_harness.events import E


async def main() -> None:
    title("04 上下文工程")
    ws = temp_workspace("04")
    (ws / "app.log").write_text("\n".join(f"2026-10-13 10:{i // 60:02d}:{i % 60:02d} INFO 处理请求 #{i} 耗时 {i % 97}ms" for i in range(6000)))
    script = [{"tool_calls": [{"name": "bash", "args": {"command": "cat app.log"}}]}]
    script += [{"tool_calls": [{"name": "read", "args": {"path": "app.log", "offset": 1 + 150 * k, "limit": 150}}]} for k in range(8)]
    script.append("日志看完了：没有 ERROR，耗时分布正常。")
    h = harness(ws, ScriptedProvider(script, mode="sequential"), context__context_window=16000,
                context__reserve_tokens=2000, context__keep_recent_tokens=3000, loop_guard__stop_repeats=99)

    def on_event(ev):
        if ev.type == E.MESSAGE_START:
            c = ev.data["context"]
            bar = "█" * int(c["ratio"] * 40)
            print(f"  水位 {c['tokens']:>6} / {c['window']}  {bar:<40} {c['ratio']:.0%}")
        elif ev.type in (E.COMPACTION_START, E.COMPACTION_END, E.NOTICE):
            render(ev)
        elif ev.type == E.TOOL_END and "句柄" in ev.data["content"][:200]:
            say("大结果已外置：上下文里只有头尾预览 + 句柄")

    res = await h.run("分析 app.log", on_event=on_event)
    view = h.log(res.session_id).context()
    title("压缩后模型看到的第一条消息（摘要）")
    print(view.messages[0].text[:900])
    await h.aclose()


if __name__ == "__main__":
    asyncio.run(main())
