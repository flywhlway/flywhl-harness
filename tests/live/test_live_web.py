"""真实联网：搜索 / 读取后端的协议与计费，以及模型用联网工具回答时效性问题。

每个用例开头检查自己要的密钥，缺了就跳过。默认后端 DeepSeek Search / Fetch 每次成功调用 0.00075 美元；
全部跑完约十几次联网调用加几次模型调用，合计不到 0.05 美元。
"""

from __future__ import annotations

import os
import re

import pytest

from flywhl_harness.config import WebConfig
from flywhl_harness.events import E
from flywhl_harness.net.search import SearchQuery, WebService

from .conftest import live_harness, need, run, tools_used, transcript

pytestmark = pytest.mark.live


def _need_key(env: str) -> None:
    if not os.environ.get(env):
        pytest.skip(f"未配置 {env}")


def _service(**kw) -> WebService:
    return WebService(WebConfig(enabled=True, **kw), env=dict(os.environ))


async def test_deepseek_search_returns_priced_http_hits():
    _need_key("DEEPSEEK_API_KEY")
    svc = _service(backends=["deepseek"])
    try:
        out = await svc.search(SearchQuery("Python 最新稳定版本 发布", 5, freshness="year"))
    finally:
        await svc.aclose()
    assert out.backend == "deepseek" and out.hits, out
    assert all(re.match(r"^https?://", h.url) for h in out.hits)
    assert out.billed and out.cost_usd == pytest.approx(0.00075)


async def test_deepseek_fetch_reads_a_public_page():
    _need_key("DEEPSEEK_API_KEY")
    svc = _service(backends=["deepseek"], fetch_backend="deepseek")
    try:
        out = await svc.fetch("https://www.python.org/downloads/", in_scope=lambda url, host: True)
    finally:
        await svc.aclose()
    assert out.page.content and "Python" in out.page.content
    assert out.cost_usd == pytest.approx(0.00075)


async def test_direct_fetch_reads_a_public_page():
    svc = _service(backends=["deepseek"], fetch_backend="direct")
    try:
        out = await svc.fetch("https://www.python.org/downloads/", in_scope=lambda url, host: True)
    finally:
        await svc.aclose()
    assert out.page.status == 200 and "Python" in out.page.content and out.cost_usd == 0


async def test_bocha_search_returns_hits():
    _need_key("BOCHA_API_KEY")
    svc = _service(backends=["bocha"])
    try:
        out = await svc.search(SearchQuery("Python 最新稳定版本", 5))
    finally:
        await svc.aclose()
    assert out.backend == "bocha" and out.hits, out


async def test_model_answers_a_timely_question_with_sources(ws):
    """仅搜索：模型应当先搜再答，回答里带上搜索结果中出现过的网址；会话进入污点态，计数与费用落进结果。"""
    need("deepseek:deepseek-flash")
    h = live_harness(ws, model="deepseek:deepseek-flash", web__enabled=True, web__default="search",
                     web__backends=["deepseek"])
    res = await run(h, "Python 当前最新的稳定版本号是多少？先联网搜索再回答，回答里附上来源网址。", label="联网问答")
    tr = transcript(h, res.session_id)
    assert res.status == "completed", tr
    assert "web_search" in tools_used(res), tr
    urls = {x["url"] for e in res.events if e.type == E.TOOL_END and e.data.get("name") == "web_search"
            for x in (e.data.get("details") or {}).get("hits") or []}
    assert any(u in res.text for u in urls), tr
    run_rec = h.store.get_run(res.run_id)
    assert run_rec.result["web_searches"] >= 1 and run_rec.result["tool_cost_usd"] > 0
    assert h.store.get_session(res.session_id).meta["tainted"] is True
