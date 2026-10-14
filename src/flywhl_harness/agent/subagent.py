"""子代理委托：task 工具 + 结构化回传契约。

为什么要子代理？三个正当理由：**上下文隔离**（高噪声探索不污染主上下文）、**并行**、**专业化/更窄权限**。
代价是信息损失与 token 成本（多代理的 token 消耗通常是单代理的数倍），所以要用评测证明净收益。

为什么要严格的回传契约？
- 父代理需要的是“结论 + 证据 + 产物位置”，而不是子代理的完整对话；
- 自然语言回传会让父代理“猜”——多层转述还会像传话游戏一样失真；
- 契约可以被 **机器校验**：schema 校验 + 产物存在性校验；违约时让子代理修正一次，仍违约按 failed 处理。

隔离与治理：子代理拿到全新的会话（看不到父上下文）、更窄的工具集（嵌套深度受 ``subagent_max_depth`` 限制：
默认 2，即子代理还能再委托一层，到达上限的那一层拿不到 task 工具）、
从父预算切出的子预算、父取消令牌的子令牌（父取消 → 子立即取消）。
"""

from __future__ import annotations

import json
from typing import Any

from .._utils import extract_json_block
from ..tools.base import Effect, Tool, ToolContext, ToolResult
from ..tools.schema import validate

CONTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["ok", "partial", "failed", "needs_input"]},
        "summary": {"type": "string", "maxLength": 2000},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["claim"],
            },
        },
        "artifacts": {"type": "array", "items": {"type": "string"}},
        "open_questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status", "summary"],
}


def check_contract(text: str, workspace: Any) -> tuple[dict[str, Any] | None, list[str]]:
    """解析并校验回传契约。返回 (契约, 违约项)。"""
    data = extract_json_block(text)
    if data is None:
        return None, ["最后一条回复中没有找到 JSON 对象"]
    data, errors = validate(CONTRACT_SCHEMA, data)
    if errors:
        return data, errors
    missing = [a for a in data.get("artifacts", []) if not workspace.resolve(a, allow_outside=True).exists()]
    if missing:
        data.setdefault("open_questions", []).append(f"声明的产物不存在：{missing}")
        if data["status"] == "ok":
            data["status"] = "partial"
    return data, []


class TaskTool(Tool):
    name = "task"
    description = (
        "把一个边界清晰的子任务委托给子代理，在隔离的上下文中执行，只取回结构化结论（status/summary/findings/artifacts）。"
        "适合大量搜索与阅读、只需要结论的探索性工作。子代理看不到当前对话，prompt 中必须写清目标、范围、输入与期望输出。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "3-8 个字的任务名，如“定位配置入口”"},
            "prompt": {"type": "string", "description": "完整的任务简报：目标、范围与边界、输入（文件路径/句柄）、约束、期望输出"},
            "tools": {"type": "array", "items": {"type": "string"}, "description": "可选：限定子代理可用的工具名"},
            "max_turns": {"type": "integer", "minimum": 1, "maximum": 60, "default": 15},
        },
        "required": ["description", "prompt"],
        "additionalProperties": False,
    }
    effect = Effect.READ        # 子代理内部的副作用由其自身的权限与审批把关
    # 不可中断：父 Run 取消时，子 Run 通过子取消令牌协作式地停下并写入终态；
    # 若直接抢占，子 Run 会永远停在 running（它的收尾代码来不及执行）
    interruptible = False
    cancel_grace = 30.0
    timeout = 1800

    def describe_call(self, args: dict[str, Any]) -> str:
        return f"子代理：{args.get('description', '')}"

    def permission_target(self, args: dict[str, Any]) -> str:
        return str(args.get("description", ""))

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.harness is None or ctx.run is None:
            return ToolResult.error("当前运行环境不支持子代理")
        parent = ctx.run
        if parent.depth + 1 > parent.config.subagent_max_depth:
            return ToolResult.error(f"子代理嵌套深度已达上限 {parent.config.subagent_max_depth}，请直接完成该任务。")
        result = await ctx.harness.run_subagent(
            parent, description=args["description"], prompt=args["prompt"], tools=args.get("tools"),
            max_turns=args.get("max_turns", 15), emit=ctx.emit,
        )
        contract, text = result["contract"], json.dumps(result["contract"], ensure_ascii=False, indent=1)
        return ToolResult(
            content=f"子代理「{args['description']}」回传（status={contract.get('status')}）：\n{text}",
            is_error=contract.get("status") == "failed",
            details={k: v for k, v in result.items() if k != "contract"} | {"contract": contract},
        )
