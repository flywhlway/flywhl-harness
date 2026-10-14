"""06 · 子代理：隔离上下文 + 结构化回传契约 + 违约修正。

运行：python examples/06_subagent.py

观察三点：
1. 子代理在全新的会话里工作——它看不到父会话的历史，只有任务简报；
2. 子代理第一次回传了自然语言（违约）→ harness 校验失败 → 要求它只输出契约 JSON → 第二次通过；
3. 父上下文里只有契约，没有子代理的 ls / grep 过程——这就是“上下文隔离”。
"""

import asyncio
import json

from _common import harness, say, temp_workspace, title

from flywhl_harness import ScriptedProvider
from flywhl_harness.llm.base import Provider

CONTRACT = {"status": "ok", "summary": "入口是 app/main.py，配置从 config.toml 读取",
            "findings": [{"claim": "main.py 调用 load_config()", "evidence": ["app/main.py:3"], "confidence": 0.9}],
            "artifacts": ["app/main.py"], "open_questions": []}


class Router(Provider):
    name = "mock"

    def __init__(self):
        self.main = ScriptedProvider([
            {"text": "这需要大量搜索，交给子代理。", "tool_calls": [{"name": "task", "args": {
                "description": "定位入口与配置", "prompt": "在工作区中找到程序入口文件与配置加载方式。输出契约 JSON。"}}]},
            lambda req: "子代理回报：" + json.loads(req.messages[-1].text.split("：", 1)[1])["summary"],
        ])
        self.sub = ScriptedProvider([
            {"tool_calls": [{"name": "ls", "args": {"path": ".", "depth": 2}}]},
            {"tool_calls": [{"name": "grep", "args": {"pattern": "load_config"}}]},
            "我找到了，入口应该是 main.py。",                               # 违约：不是 JSON
            "```json\n" + json.dumps(CONTRACT, ensure_ascii=False) + "\n```",
        ])
        self.sub_saw_parent_history = None

    async def stream(self, req, cancel):
        is_sub = req.metadata.get("role") == "subagent"
        if is_sub and self.sub_saw_parent_history is None:
            self.sub_saw_parent_history = any("这需要大量搜索" in m.text for m in req.messages)
        async for ev in (self.sub if is_sub else self.main).stream(req, cancel):
            yield ev


async def main() -> None:
    title("06 子代理")
    ws = temp_workspace("06")
    (ws / "app").mkdir()
    (ws / "app" / "main.py").write_text("from config import load_config\n\ncfg = load_config()\n")
    prov = Router()
    h = harness(ws, prov)

    def on_event(ev):
        if ev.type == "tool_update" and "subagent" in ev.data["update"]:
            sub = ev.data["update"]["subagent"]["event"]
            if sub["type"] in ("tool_start", "run_end"):
                d = sub["data"]
                print(f"    [子代理] {sub['type']}: {d.get('name') or d.get('status')}")

    res = await h.run("帮我搞清楚这个项目的入口和配置", on_event=on_event)
    end = next(e for e in res.events if e.type == "tool_end")
    say(f"子代理是否看到了父会话历史：{prov.sub_saw_parent_history}（应为 False）")
    say(f"子 Run 次数：{len(end.data['details']['child_run_ids'])}（第一次违约，修正一次）")
    print("  回传契约：", json.dumps(end.data["details"]["contract"], ensure_ascii=False))
    parent_tools = [m.tool_name for m in h.log(res.session_id).messages() if m.role == "tool"]
    say(f"父上下文中的工具结果：{parent_tools}（只有 task，没有子代理的 ls/grep）")
    print(f"  最终回答：{res.text}")
    await h.aclose()


if __name__ == "__main__":
    asyncio.run(main())
