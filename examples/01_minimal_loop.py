"""01 · 最小 Agent Loop：自定义工具 + 事件流 + 五种异常情况。

运行：python examples/01_minimal_loop.py

你会看到：
1. 用 @tool 装饰器 10 行定义一个工具（schema 从函数签名自动推导）；
2. 模型一次返回两个只读调用 → 并行执行，结果按原序回填；
3. 未知工具、参数不合 schema、参数非法 JSON、工具抛异常——全部变成模型可读的错误，Loop 不中断；
4. 最后打印消息历史：每个 tool_call 都有与之配对的 tool 结果。
"""

import asyncio
from typing import Annotated

from _common import harness, render, say, temp_workspace, title

from flywhl_harness import Effect, ScriptedProvider, tool


@tool(effect=Effect.READ)
async def weather(city: Annotated[str, "城市名，如 上海"], unit: Annotated[str, "温度单位"] = "C") -> str:
    """查询城市当前天气（演示用的假数据）。"""
    if city == "火星":
        raise RuntimeError("火星气象站离线")
    return f"{city}：晴，23°{unit}"


SCRIPT = [
    {"thinking": "两个城市互不依赖，可以并行查询。", "text": "我同时查一下两个城市。",
     "tool_calls": [{"name": "weather", "args": {"city": "上海"}}, {"name": "weather", "args": {"city": "北京"}}]},
    {"text": "再试几个边界情况。", "tool_calls": [
        {"name": "weather_v2", "args": {"city": "上海"}},           # 未知工具
        {"name": "weather", "args": {"unit": "F"}},                # 缺少必填参数
        {"name": "weather", "raw": '{"city": "广州"'},              # 非法 JSON（被截断）
        {"name": "weather", "args": {"city": "火星"}},              # 工具抛异常
    ]},
    "上海、北京都是晴天 23°C；其余调用的错误信息我都读到了，可以据此修正。",
]


async def main() -> None:
    title("01 最小 Agent Loop")
    h = harness(temp_workspace("01"), ScriptedProvider(SCRIPT, delay=0.004))
    h.tools.register(weather)
    say(f"自动推导的参数 schema：{weather.parameters}")
    res = await h.run("查一下上海和北京的天气", on_event=render)
    title("消息历史（tool_call ↔ tool_result 一一配对）")
    for m in h.log(res.session_id).messages():
        calls = ", ".join(f"{c.name}#{c.id}" for c in m.tool_calls)
        extra = f" → 调用 [{calls}]" if calls else (f" ← 结果 #{m.tool_call_id}{' ✗' if m.is_error else ''}" if m.role == "tool" else "")
        print(f"  {m.role:<9}{extra}  {m.text[:60]!r}")
    await h.aclose()


if __name__ == "__main__":
    asyncio.run(main())
