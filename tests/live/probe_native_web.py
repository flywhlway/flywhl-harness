"""探测模型侧联网的两个未核实问题，决定 llm/native.py 里对应声明的 ``with_tools`` 能不能改成 True：

1. DeepSeek 的 Anthropic 兼容端点：``web_search`` server tool 与函数工具同时发送，请求会不会被拒、搜索有没有发生；
2. 百炼 ``enable_search``（OpenAI 兼容方式）：与函数工具同时发送，请求会不会被拒、回答里有没有当天的信息。

    .venv/bin/python tests/live/probe_native_web.py      # 读取项目根目录的 .env，缺密钥的那一项跳过

只打印观察结果（不打印密钥与完整回答），每项一次调用，合计约 0.01 美元。结论要人看过再改声明。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from flywhl_harness import HarnessConfig  # noqa: E402
from flywhl_harness.cancel import CancelToken  # noqa: E402
from flywhl_harness.config import load_dotenv  # noqa: E402
from flywhl_harness.llm import create_provider  # noqa: E402
from flywhl_harness.llm.base import ModelRequest  # noqa: E402
from flywhl_harness.types import Message, ServerBlock, TextBlock  # noqa: E402

TOOL = {"name": "get_time", "description": "返回当前时间", "parameters": {"type": "object", "properties": {}}}
QUESTION = "今天的国内科技新闻头条是什么？请联网搜索后用一句话回答，不需要调用 get_time。"


async def probe(label: str, model: str, native: str, **cfg) -> dict:
    try:
        provider, name = create_provider(HarnessConfig(model=model, max_retries=0, **cfg))
    except Exception as e:   # 缺密钥等
        return {"probe": label, "skipped": str(e)[:200]}
    req = ModelRequest(model=name, system="你是测试助手。", messages=[Message(role="user", content=[TextBlock(QUESTION)])],
                       tools=[TOOL], max_tokens=800, native_search=native, native_opts={"max_uses": 2})
    try:
        evs = [ev async for ev in provider.stream(req, CancelToken())]
    except Exception as e:
        return {"probe": label, "accepted": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}
    finally:
        await provider.aclose()
    msg = evs[-1].message
    servers = [c.data.get("type") for c in msg.content if isinstance(c, ServerBlock)]
    return {"probe": label, "accepted": True, "stop_reason": msg.stop_reason, "server_blocks": servers,
            "native_web": msg.meta.get("native_web"), "tool_calls": [c.name for c in msg.tool_calls],
            "answer_head": msg.text[:120], "usage": msg.usage.to_dict() if msg.usage else None}


async def main() -> None:
    load_dotenv(ROOT / ".env")
    rows = await asyncio.gather(
        probe("deepseek-anthropic + 函数工具", "anthropic:deepseek-v4-pro", "deepseek-anthropic",
              base_url="https://api.deepseek.com/anthropic", api_key_env="DEEPSEEK_API_KEY", thinking="disabled"),
        probe("百炼 enable_search + 函数工具", "dashscope:qwen3.8-flash", "dashscope", thinking="disabled"),
    )
    for r in rows:
        print(json.dumps(r, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
