"""无需 API Key 的模型：脚本化模型（测试/示例）与规则演示模型（Web UI 开箱即用）。

为什么 harness 需要 mock 模型？
- **可测试**：Loop、取消、恢复、压缩这些机制必须能确定性地复现，测试里不能依赖真实模型；
- **可演示**：没有 Key 也能完整体验工具调用、审批、取消、压缩、子代理等全部流程；
- **可回放**：ReplayProvider（见 replay.py）本质上也是一种 mock——按录制的轨迹重放。

ScriptedProvider 默认按“请求中 assistant 消息的数量”选择脚本条目，因此进程崩溃重启后
仍能从正确的位置继续——这正是崩溃恢复演示依赖的性质。
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, AsyncIterator, Callable

from .._utils import clip, estimate_tokens
from ..cancel import CancelToken
from ..types import Message, StopReason, Usage
from .base import MessageBuilder, ModelRequest, Provider, StreamEvent

Turn = str | dict[str, Any] | Exception | Callable[[ModelRequest], Any]


def _estimate_input(req: ModelRequest) -> int:
    total = estimate_tokens(req.system) + estimate_tokens(json.dumps(req.tools, ensure_ascii=False))
    for m in req.messages:
        total += estimate_tokens(m.text) + estimate_tokens(m.thinking)
        for c in m.tool_calls:
            total += estimate_tokens(json.dumps(c.arguments, ensure_ascii=False)) + 8
    return total


async def _emit_spec(
    spec: dict[str, Any], req: ModelRequest, cancel: CancelToken, *, provider: str, chunk: int, delay: float, id_prefix: str
) -> AsyncIterator[StreamEvent]:
    """把一个回合规格（text/thinking/tool_calls）以流式事件的形式吐出来。"""
    b = MessageBuilder(provider, req.model)
    yield StreamEvent("start")
    idx = 0
    if spec.get("thinking"):
        for i in range(0, len(spec["thinking"]), chunk):
            cancel.raise_if_cancelled()
            piece = spec["thinking"][i : i + chunk]
            b.thinking(idx, piece)
            yield StreamEvent("thinking_delta", piece, idx)
            if delay:
                await asyncio.sleep(delay)
        b.signature(idx, "mock-signature")
        idx += 1
    text = spec.get("text") or ""
    if text:
        for i in range(0, len(text), chunk):
            cancel.raise_if_cancelled()
            piece = text[i : i + chunk]
            b.text(idx, piece)
            yield StreamEvent("text_delta", piece, idx)
            if delay:
                await asyncio.sleep(delay)
        idx += 1
    for j, tc in enumerate(spec.get("tool_calls") or []):
        call_id = tc.get("id") or f"{id_prefix}_{j}"
        b.tool_start(idx, call_id, tc["name"])
        yield StreamEvent("toolcall_start", index=idx, info={"id": call_id, "name": tc["name"]})
        raw = tc["raw"] if "raw" in tc else json.dumps(tc.get("args", {}), ensure_ascii=False)
        for i in range(0, len(raw), max(chunk * 3, 12)):
            cancel.raise_if_cancelled()
            piece = raw[i : i + max(chunk * 3, 12)]
            b.tool_delta(idx, piece)
            yield StreamEvent("toolcall_delta", piece, idx)
            if delay:
                await asyncio.sleep(delay / 2)
        yield StreamEvent("toolcall_end", index=idx, tool_call=b.tool_call(idx))
        idx += 1
    stop = spec.get("stop_reason") or (StopReason.TOOL_USE if spec.get("tool_calls") else StopReason.END_TURN)
    out_tokens = estimate_tokens(text) + estimate_tokens(spec.get("thinking", "")) + 20 * len(spec.get("tool_calls") or [])
    usage = Usage(**spec["usage"]) if spec.get("usage") else Usage(input_tokens=_estimate_input(req), output_tokens=out_tokens)
    yield StreamEvent("done", message=b.build(stop, usage))


def deterministic_summary(req: ModelRequest) -> str:
    """不调用模型的“摘要”：从待压缩的文本里抽取首尾要点。用于 mock 与无模型兜底。"""
    src = req.messages[-1].text if req.messages else ""
    m = re.search(r"<conversation>\n?(.*?)\n?</conversation>", src, re.S)
    if m:
        src = m.group(1)
    lines = [ln for ln in src.splitlines() if ln.strip()]
    goal = next((ln for ln in lines if ln.startswith("[用户]")), lines[0] if lines else "（无）")
    tools = [ln for ln in lines if ln.startswith("[工具调用]")]
    tail = lines[-6:]
    return (
        "## 目标\n" + clip(goal.replace("[用户]:", "").strip(), 300) + "\n\n"
        "## 进展\n" + ("\n".join(f"- {clip(t, 160)}" for t in tools[-8:]) or "- （无工具调用）") + "\n\n"
        "## 关键上下文\n" + "\n".join(f"- {clip(t, 160)}" for t in tail) + "\n\n"
        "## 下一步\n- 基于以上进展继续完成目标"
    )


class ScriptedProvider(Provider):
    """按脚本回放的模型。脚本条目可以是：

    - ``str``：一段文本回复；
    - ``dict``：``{"text", "thinking", "tool_calls": [{"name", "args"}], "stop_reason", "usage", "delay"}``；
    - ``Exception``：直接抛出（测试重试/降级）；
    - ``callable(req)``：根据请求动态生成以上任意一种。
    """

    name = "mock"

    def __init__(self, script: list[Turn], *, chunk: int = 8, delay: float = 0.0, mode: str = "assistant_count"):
        self.script = list(script)
        self.chunk = chunk
        self.delay = delay
        self.mode = mode          # assistant_count：按请求中的 assistant 数选择；sequential：按调用次数
        self.calls = 0
        self.requests: list[ModelRequest] = []  # 记录收到的请求，测试中用来断言“模型看到了什么”

    def _pick(self, req: ModelRequest) -> Turn:
        if self.mode == "sequential":
            i = self.calls
        else:
            i = sum(1 for m in req.messages if m.role == "assistant" and m.stop_reason != StopReason.ERROR)
        return self.script[i] if i < len(self.script) else "（脚本已结束）"

    async def stream(self, req: ModelRequest, cancel: CancelToken) -> AsyncIterator[StreamEvent]:
        self.requests.append(req)
        if req.purpose == "compaction":
            spec: Any = {"text": deterministic_summary(req)}
        else:
            spec = self._pick(req)
            self.calls += 1
        if callable(spec) and not isinstance(spec, Exception):
            spec = spec(req)
        if isinstance(spec, Exception):
            raise spec
        if isinstance(spec, str):
            spec = {"text": spec}
        if spec.get("delay_before"):
            await cancel.sleep(spec["delay_before"])
        async for ev in _emit_spec(spec, req, cancel, provider=self.name, chunk=self.chunk,
                                   delay=spec.get("delay", self.delay), id_prefix=f"call_{len(self.requests)}"):
            yield ev


# ====================================================================== 规则演示模型
_HELP = """我是 **flywhl-demo**：一个内置的规则演示模型，不需要 API Key，用来体验 harness 的各项机制。试试这些说法：

| 说法 | 演示的机制 |
|---|---|
| `列出目录` | 只读工具、自动放行 |
| `读取 README.md` | 带行号读取、截断提示 |
| `搜索 def` | grep 搜索 |
| `创建 hello.py` | 写文件（default 模式下需审批）、影子 git 快照 |
| ``运行 `python hello.py` `` | bash 命令风险分析、审批、流式输出 |
| `发送通知` | 外部副作用工具、幂等键 |
| `等待 20 秒` | 可中断工具，试试中途点“停止” |
| `并行检查` | 同一轮多个只读调用并发执行、按原序回填 |
| `制定计划` | todo_write 与计划复述（system-reminder） |
| `委托子任务` | 子代理：隔离上下文 + 结构化回传契约 |
| `循环测试` | 重复调用检测（loop guard） |
| `压缩演示` | 上下文水位上升 → 自动压缩 |
| `错误演示` | 工具报错 → 模型读到错误后自我修正 |

接入真实模型：设置 `ANTHROPIC_API_KEY`，或 `OPENAI_API_KEY` + `FLYWHL_BASE_URL`（DeepSeek、通义、Ollama 等 OpenAI 兼容服务）。"""


def _call(name: str, **args: Any) -> dict[str, Any]:
    return {"name": name, "args": args}


def _plan_for(prompt: str) -> tuple[str, list[list[dict[str, Any]]]]:
    """根据用户输入生成 (思考, 多轮工具调用计划)。每个内层列表是同一轮内的调用。"""
    p = prompt.strip()
    low = p.lower()
    path_m = re.search(r"([\w./\-]+\.[A-Za-z0-9]{1,6})", p)
    path = path_m.group(1) if path_m else None
    cmd_m = re.search(r"`([^`]+)`", p)
    secs_m = re.search(r"(\d+(?:\.\d+)?)\s*(秒|s\b|sec)", low)

    if re.search(r"帮助|help|你能做什么|你是谁|功能", low):
        return "", []
    if re.search(r"循环|loop|重复", low):
        return "用户想测试循环检测：我会反复执行同一个调用。", [[_call("ls", path=".")] for _ in range(7)]
    if re.search(r"压缩|compact|长上下文", low):
        gen = ("python3 -c \"[print(f'第 {i:04d} 行：这是一段用于撑大上下文的演示数据，编号 {i}，' + 'x' * (i % 23)) "
               "for i in range(1, 801)]\" > demo_big.txt && wc -l demo_big.txt")
        return "用户想观察上下文压缩：先用命令生成一个大文件，再分段多次读取，让上下文水位逐轮上升。", [
            [_call("bash", command=gen)],
            # 每次读 120 行（约 5K tokens）：增长速度快于“旧结果清理”的回收速度，先看到清理、再看到压缩
            *[[_call("read", path="demo_big.txt", offset=1 + 120 * k, limit=120)] for k in range(7)],
        ]
    if re.search(r"错误|error|报错", low):
        return "我先尝试读取一个不存在的文件，再根据错误信息调整策略。", [
            [_call("read", path="not_exist_file.txt")],
            [_call("ls", path=".")],
        ]
    if re.search(r"并行|parallel", low):
        return "这三个检查互不依赖且都是只读的，可以放在同一轮里并行执行。", [[
            _call("ls", path="."), _call("grep", pattern="TODO|FIXME", max_results=20), _call("read", path=path or "README.md", limit=40),
        ]]
    if re.search(r"子任务|委托|subagent|子代理|task", low):
        return "这类探索会产生大量噪声，适合委托给子代理在隔离上下文中完成，只取回结构化结论。", [[
            _call("task", description="盘点工作区", prompt="请列出工作区的顶层结构，找出可能的入口文件，并以契约 JSON 回传结论。"),
        ]]
    if re.search(r"计划|todo|多步", low):
        todos = [
            {"content": "查看目录结构", "status": "in_progress"},
            {"content": "搜索待办标记", "status": "pending"},
            {"content": "汇总结论", "status": "pending"},
        ]
        done1 = [dict(t) for t in todos]
        done1[0]["status"], done1[1]["status"] = "completed", "in_progress"
        done2 = [dict(t, status="completed") for t in todos]
        return "这是一个多步骤任务，我先建立计划，再逐项推进并及时更新状态。", [
            [_call("todo_write", todos=todos)],
            [_call("ls", path=".")],
            [_call("todo_write", todos=done1)],
            [_call("grep", pattern="TODO|FIXME", max_results=20)],
            [_call("todo_write", todos=done2)],
        ]
    if re.search(r"通知|notify|发送", low):
        return "这是一个外部副作用操作，harness 会要求审批，并为它生成幂等键。", [[
            _call("send_notification", to="ops-team", message=f"来自 flywhl 的通知：{clip(p, 60)}"),
        ]]
    if re.search(r"等待|sleep|睡", low):
        secs = float(secs_m.group(1)) if secs_m else 15
        return f"我将等待 {secs:g} 秒；这个工具是可中断的，你可以随时取消。", [[_call("sleep", seconds=secs)]]
    if cmd_m or re.search(r"运行|执行|run\b", low):
        cmd = cmd_m.group(1) if cmd_m else "ls -la"
        return f"用户要求执行命令 `{cmd}`。harness 会先分析命令风险再决定是否需要审批。", [[_call("bash", command=cmd)]]
    if re.search(r"创建|写入|新建|write|create", low):
        target = path or "hello.py"
        content = (
            '"""由 flywhl-demo 创建的示例文件。"""\n\n\ndef greet(name: str) -> str:\n'
            '    return f"你好，{name}！"\n\n\nif __name__ == "__main__":\n    print(greet("harness"))\n'
        )
        return f"我将创建 {target}，写操作会在执行前打快照，便于回滚。", [
            [_call("write", path=target, content=content)],
            [_call("read", path=target)],
        ]
    if re.search(r"修改|替换|edit", low) and path:
        return f"先读取 {path} 确认精确内容，再做最小化替换。", [
            [_call("read", path=path)],
            [_call("edit", path=path, old_string="harness", new_string="flywhl harness")],
        ]
    if re.search(r"读取|查看|打开|read|cat", low) and path:
        return f"读取 {path}。", [[_call("read", path=path)]]
    if re.search(r"搜索|查找|grep|search", low):
        kw = cmd_m.group(1) if cmd_m else (re.sub(r"搜索|查找|grep|search", "", p).strip().split() or ["def"])[0]
        return f"在工作区中搜索 {kw}。", [[_call("grep", pattern=re.escape(kw), max_results=50)]]
    if re.search(r"列出|目录|文件|ls|结构", low):
        return "查看工作区顶层结构。", [[_call("ls", path=".", depth=2)]]
    return "", []


class DemoProvider(Provider):
    """规则驱动的演示模型：把常见说法映射为工具调用计划，并按回合推进。"""

    name = "mock"

    def __init__(self, delay: float = 0.012, chunk: int = 3):
        self.delay = delay
        self.chunk = chunk
        # 按 (run_id, 输入) 记录计划推进到第几步：压缩会改写历史，不能只靠“数 assistant 消息”
        self._progress: dict[tuple[str, str], int] = {}

    async def stream(self, req: ModelRequest, cancel: CancelToken) -> AsyncIterator[StreamEvent]:
        spec = self._respond(req)
        async for ev in _emit_spec(spec, req, cancel, provider=self.name, chunk=self.chunk, delay=self.delay,
                                   id_prefix=f"call_{abs(hash((len(req.messages), req.messages[-1].id if req.messages else '')))%10**8}"):
            yield ev

    def _respond(self, req: ModelRequest) -> dict[str, Any]:
        if req.purpose == "compaction":
            return {"text": deterministic_summary(req)}
        msgs = req.messages
        # 找到最近一次“真实”的用户输入（prompt / steer / follow_up）
        start = max((i for i, m in enumerate(msgs) if m.role == "user" and m.kind in ("prompt", "steer", "follow_up", None)), default=-1)
        prompt = msgs[start].text if start >= 0 else ""
        if not prompt:  # 原始输入已被压缩进摘要：从摘要的“原始任务（原文）”一节找回
            summary = next((m.text for m in msgs if m.kind == "compaction"), "")
            found = re.search(r"## 原始任务（原文）\n(.+?)(?:\n\n|\n<|$)", summary, re.S)
            prompt = found.group(1).strip() if found else ""
        key = (str(req.metadata.get("run_id", "")), prompt)
        step = self._progress.get(key, sum(1 for m in msgs[start + 1 :] if m.role == "assistant"))
        if req.purpose == "turn":
            self._progress[key] = step + 1
        is_sub = req.metadata.get("role") == "subagent"

        if req.purpose == "wrap_up" or req.tool_choice == "none":
            return {"text": "预算即将耗尽，我在这里收尾：\n\n" + self._summarize_results(msgs[start + 1 :]) + "\n\n未完成的部分可以在新的运行中继续。"}
        if is_sub:
            return self._subagent(prompt, step, msgs[start + 1 :])

        thought, plan = _plan_for(prompt)
        if not plan:
            if step == 0 and not re.search(r"帮助|help|你能做什么|你是谁|功能", prompt.lower()) and prompt:
                return {"text": f"收到：「{clip(prompt, 80)}」。\n\n" + _HELP}
            return {"text": _HELP}
        if step < len(plan):
            lead = {0: "好的，我来处理。"}.get(step, "")
            # 循环演示时每轮说同样的话：这正是“死循环”的样子
            return {"thinking": thought if step == 0 else "", "text": lead, "tool_calls": plan[step]}
        return {"text": "完成。以下是执行结果摘要：\n\n" + self._summarize_results(msgs[start + 1 :])}

    def _subagent(self, brief: str, step: int, msgs: list[Message]) -> dict[str, Any]:
        if step == 0:
            return {"text": "子代理开始盘点。", "tool_calls": [_call("ls", path=".", depth=1)]}
        if step == 1:
            return {"text": "", "tool_calls": [_call("grep", pattern=r"if __name__|def main|app\s*=", max_results=10)]}
        files = []
        for m in msgs:
            if m.role == "tool" and m.tool_name == "ls":
                files = [ln.split("  (")[0] for ln in m.text.splitlines()[:12]]
        hits = [m.text for m in msgs if m.role == "tool" and m.tool_name == "grep"]
        contract = {
            "status": "ok",
            "summary": f"工作区顶层共有 {len(files)} 项（已截取前 12 项），入口候选见 findings。",
            "findings": [
                {"claim": "顶层条目：" + ", ".join(files[:8]), "evidence": ["tool:ls"], "confidence": 0.9},
                {"claim": "入口候选：" + clip(hits[0].splitlines()[0] if hits and hits[0] else "未找到", 120),
                 "evidence": ["tool:grep"], "confidence": 0.6},
            ],
            "artifacts": [],
            "open_questions": [],
        }
        return {"text": "```json\n" + json.dumps(contract, ensure_ascii=False, indent=2) + "\n```"}

    @staticmethod
    def _summarize_results(msgs: list[Message]) -> str:
        parts = []
        for m in msgs:
            if m.role != "tool":
                continue
            head = "\n".join(m.text.splitlines()[:8])
            mark = "✗" if m.is_error else "✓"
            parts.append(f"{mark} **{m.tool_name}**\n```\n{clip(head, 600)}\n```")
        return "\n".join(parts[-6:]) or "（本轮没有工具结果）"
