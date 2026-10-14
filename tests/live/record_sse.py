"""重新录制 tests/fixtures/sse/ 下的真实 SSE 流与错误响应（各家协议变化时运行）。

    .venv/bin/python tests/live/record_sse.py            # 读取项目根目录的 .env

只保存响应体，不保存任何请求头与密钥；每家两次调用：两个并行工具调用 → 回填结果后的最终回答。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from flywhl_harness.config import load_dotenv  # noqa: E402
from flywhl_harness.llm.profiles import PROFILES  # noqa: E402

OUT = ROOT / "tests" / "fixtures" / "sse"
MODELS = {"deepseek": "deepseek-flash", "dashscope": "qwen3.8-flash", "kimi": "kimi-k2.6",
          "minimax": "MiniMax-M2.7", "openrouter": "deepseek/deepseek-v4.1-flash"}
TOOLS = [{"type": "function", "function": {"name": "get_weather", "description": "查询城市天气",
          "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}]
QUESTION = "北京和上海今天天气怎么样？请分别调用工具查询两个城市，最后用一句话回答。"


async def record(c: httpx.AsyncClient, base: str, key: str, body: dict) -> tuple[str, dict, str, str]:
    lines, calls, reasoning, content = [], {}, "", ""
    body = {**body, "stream": True, "stream_options": {"include_usage": True}}
    async with c.stream("POST", f"{base}/chat/completions", json=body, headers={"Authorization": f"Bearer {key}"}) as r:
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {(await r.aread())[:300]!r}")
        async for line in r.aiter_lines():
            lines.append(line)
            if not line.startswith("data:") or line[5:].strip() == "[DONE]":
                continue
            for x in json.loads(line[5:]).get("choices") or []:
                d = x.get("delta") or {}
                reasoning += d.get("reasoning_content") or d.get("reasoning") or ""
                content += d.get("content") or ""
                for tc in d.get("tool_calls") or []:
                    t = calls.setdefault(tc.get("index", 0), {"id": None, "name": "", "args": ""})
                    t["id"] = t["id"] or tc.get("id")
                    fn = tc.get("function") or {}
                    t["name"] += fn.get("name") or ""
                    t["args"] += fn.get("arguments") or ""
    return "\n".join(lines) + "\n", calls, reasoning, content


async def one(name: str) -> str:
    profile, model = PROFILES[name], MODELS[name]
    key = profile.api_key()
    if not key:
        return f"{name}: 跳过（未配置 {'/'.join(profile.key_env)}）"
    async with httpx.AsyncClient(timeout=120) as c:
        msgs = [{"role": "user", "content": QUESTION}]
        raw, calls, reasoning, content = await record(c, profile.base_url, key, {"model": model, "messages": msgs,
                                                                                 "tools": TOOLS, "max_tokens": 1500})
        (OUT / f"{name}_tool_calls.sse").write_text(raw, encoding="utf-8")
        asst = {"role": "assistant", "content": content or None, "tool_calls": [
            {"id": t["id"], "type": "function", "function": {"name": t["name"], "arguments": t["args"]}} for t in calls.values()]}
        if profile.reasoning_echo == "field":
            asst["reasoning_content"] = reasoning
        results = [{"role": "tool", "tool_call_id": t["id"], "content": "晴 20°C"} for t in calls.values()]
        raw2, *_ = await record(c, profile.base_url, key, {"model": model, "messages": msgs + [asst] + results,
                                                           "tools": TOOLS, "max_tokens": 1500})
        (OUT / f"{name}_final.sse").write_text(raw2, encoding="utf-8")
    return f"{name}: 已录制（{len(calls)} 个并行调用）"


async def main() -> None:
    load_dotenv(ROOT / ".env")
    OUT.mkdir(parents=True, exist_ok=True)
    for line in await asyncio.gather(*(one(n) for n in MODELS), return_exceptions=True):
        print(line)
    print("错误响应夹具 errors.json 需手工维护：每条记录 HTTP 状态码与响应体，并在 test_recorded_errors_are_classified 中登记期望分类。")


if __name__ == "__main__":
    asyncio.run(main())
