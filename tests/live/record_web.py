"""录制联网后端的真实响应体到 tests/fixtures/web/<后端>/recorded_*.json（后端协议变化时运行）。

    .venv/bin/python tests/live/record_web.py            # 读取项目根目录的 .env

只保存响应体：不保存请求头、密钥和查询以外的请求内容。缺密钥的后端跳过。
DeepSeek 搜索与读取各一次（共 0.0015 美元），博查搜索一次。录完由离线用例 test_recorded_web_fixtures_parse 解析一遍。
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

OUT = ROOT / "tests" / "fixtures" / "web"
QUERY = "Python 最新稳定版本"
PAGE = "https://www.python.org/downloads/"

CALLS = {
    "deepseek": [("recorded_search", "https://api.deepseek.com/v1/tools/search", {"query": QUERY, "count": 5, "need_content": False}),
                 ("recorded_fetch", "https://api.deepseek.com/v1/tools/fetch", {"url": PAGE})],
    "bocha": [("recorded_search", "https://api.bochaai.com/v1/web-search", {"query": QUERY, "count": 5, "summary": True})],
}
KEYS = {"deepseek": "DEEPSEEK_API_KEY", "bocha": "BOCHA_API_KEY"}


async def record(backend: str) -> str:
    key = os.environ.get(KEYS[backend])
    if not key:
        return f"{backend}: 跳过（未配置 {KEYS[backend]}）"
    done = []
    async with httpx.AsyncClient(timeout=60) as c:
        for name, url, body in CALLS[backend]:
            r = await c.post(url, json=body, headers={"Authorization": f"Bearer {key}"})
            if r.status_code != 200:
                return f"{backend}: {name} 失败 HTTP {r.status_code}"
            (OUT / backend).mkdir(parents=True, exist_ok=True)
            (OUT / backend / f"{name}.json").write_text(json.dumps(r.json(), ensure_ascii=False, indent=2) + "\n",
                                                        encoding="utf-8")
            done.append(name)
    return f"{backend}: 已录制 {'、'.join(done)}"


async def main() -> None:
    load_dotenv(ROOT / ".env")
    for line in await asyncio.gather(*(record(b) for b in CALLS)):
        print(line)


if __name__ == "__main__":
    asyncio.run(main())
