"""联网工具的效果评测：bench/web_questions.toml 里的时效性问题，每题一个新会话。量三件事：

- **带可打开来源的回答占比**：回答里至少有一个网址，它在本题的搜索结果或读取记录里出现过（不是模型编的），
  并且现在能打开（GET 状态码小于 400，经过与 web_fetch 相同的出站检查）；
- **平均搜索次数**：每题实际发出的搜索（缓存命中不算）；
- **平均花费**：模型费用加联网工具费用，美元。

    .venv/bin/python -m bench.web_eval --out /tmp/web-eval                     # 默认 deepseek:deepseek-flash、仅搜索
    .venv/bin/python -m bench.web_eval --out /tmp/web-eval --level fetch --limit 3

只给模型 ``web_search`` / ``web_fetch`` 两个工具（没有 bash、没有写文件），所以不需要一次性容器：本机不会执行模型
给出的任何命令。开跑前核对工具名单，多出别的工具就拒绝。密钥读项目根目录的 ``.env``；结果写进 ``--out``：
每题一行 ``results.jsonl``，汇总 ``summary.json``。答案对不对不判：时效性问题的标准答案每天都在变。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

from flywhl_harness import Harness, HarnessConfig, Store
from flywhl_harness.config import load_dotenv
from flywhl_harness.events import E
from flywhl_harness.net.client import WebClient, WebError
from flywhl_harness.tools import default_tools

ROOT = Path(__file__).resolve().parents[1]
QUESTIONS = Path(__file__).with_name("web_questions.toml")
WEB_TOOLS = {"web_search", "web_fetch"}
_URL = re.compile(r"https?://[^\s<>\"'()\[\]，。；、）》]+")


def load_questions(path: Path = QUESTIONS) -> list[dict[str, str]]:
    qs = tomllib.loads(path.read_text(encoding="utf-8"))["q"]
    ids = [q["id"] for q in qs]
    if len(set(ids)) != len(ids):
        raise SystemExit("题目 id 重复")
    return qs


def answer_urls(text: str) -> list[str]:
    return list(dict.fromkeys(u.rstrip(".,:;!?") for u in _URL.findall(text or "")))


def seen_urls(events: list) -> set[str]:
    """本题里模型真正看到过的网址：搜索结果、读取的网址与最终网址。"""
    out: set[str] = set()
    for e in events:
        if e.type != E.TOOL_END:
            continue
        d = e.data.get("details") or {}
        out.update(x["url"] for x in d.get("hits") or [] if x.get("url"))
        out.update(u for u in (d.get("url"), d.get("final_url")) if u)
    return out


def _norm(u: str) -> str:
    return u.rstrip("/").split("#", 1)[0]


async def openable(client: WebClient, url: str) -> bool:
    try:
        r = await client.request("GET", url, in_scope=lambda host, u: True, redirects=5, max_bytes=200_000,
                                 truncate=True, timeout=15)
    except WebError:
        return False
    return r.status < 400


def build(level: str, model: str, data: Path, max_cost: float = 0.05) -> Harness:
    cfg = HarnessConfig.load(path=data / "no-such-config.toml", workspace=str(data))
    cfg.model, cfg.fallback_models, cfg.snapshots = model, [], False
    cfg.budget.max_cost_usd = max_cost   # 每题的上限，模型与联网费用合计；到了就收尾，一道题失控不会拖垮整次评测的花费
    cfg.permissions.on_ask = "allow"   # fetch_scope 的确认：评测里没人点，按放行处理（读取范围另有出站检查）
    cfg.web.enabled, cfg.web.default = True, level
    tools = default_tools(cfg, data)
    tools = tools.subset([t.name for t in tools if t.name in WEB_TOOLS])
    names = {t.name for t in tools}
    if not names or not names <= WEB_TOOLS:
        raise SystemExit(f"拒绝运行：评测只允许联网工具，实际是 {sorted(names)}")
    return Harness(cfg, tools=tools, store=Store(data / "harness.db"))


async def one(h: Harness, q: dict[str, str], client: WebClient) -> dict[str, Any]:
    async with asyncio.timeout(300):
        res = await h.run(q["text"] + "\n回答时附上来源网址。", title=q["id"])
    if {e.data["name"] for e in res.events if e.type == E.TOOL_START} - WEB_TOOLS:
        raise SystemExit("拒绝继续：模型调用了联网工具以外的工具")
    run = h.store.get_run(res.run_id)
    result = run.result or {}
    seen = {_norm(u) for u in seen_urls(res.events)}
    cited = [u for u in answer_urls(res.text) if _norm(u) in seen]
    opened = [u for u in cited if await openable(client, u)]
    return {"id": q["id"], "status": res.status, "searches": result.get("web_searches", 0),
            "fetches": result.get("web_fetches", 0), "model_usd": (res.usage or {}).get("cost_usd", 0.0),
            "tool_usd": result.get("tool_cost_usd", 0.0), "cited": cited, "openable": opened,
            "answer_head": res.text[:300]}


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows) or 1
    cost = [r["model_usd"] + r["tool_usd"] for r in rows]
    return {"questions": len(rows), "completed": sum(r["status"] == "completed" for r in rows),
            "with_openable_source": round(sum(bool(r["openable"]) for r in rows) / n, 3),
            "avg_searches": round(sum(r["searches"] for r in rows) / n, 2),
            "avg_fetches": round(sum(r["fetches"] for r in rows) / n, 2),
            "avg_cost_usd": round(sum(cost) / n, 6), "total_cost_usd": round(sum(cost), 6)}


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--model", default="deepseek:deepseek-flash")
    ap.add_argument("--level", choices=["search", "fetch"], default="search")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 题")
    ap.add_argument("--max-cost", type=float, default=0.05, help="每题费用上限（美元，模型加联网）")
    a = ap.parse_args(argv)
    load_dotenv(ROOT / ".env")
    qs = load_questions()[: a.limit or None]
    a.out.mkdir(parents=True, exist_ok=True)
    h = build(a.level, a.model, a.out, a.max_cost)
    client = WebClient(timeout=15)
    rows = []
    try:
        async with h:
            for q in qs:
                row = await one(h, q, client)
                rows.append(row)
                print(f"{q['id']:<18} {row['status']:<10} 搜索 {row['searches']} 读取 {row['fetches']} "
                      f"来源 {len(row['openable'])}/{len(row['cited'])} ${row['model_usd'] + row['tool_usd']:.4f}", flush=True)
    finally:
        await client.aclose()
    with open(a.out / "results.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    s = {"model": a.model, "level": a.level, **summarize(rows)}
    (a.out / "summary.json").write_text(json.dumps(s, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(s, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
