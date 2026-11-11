"""联网效果评测（bench/web_eval.py）的离线部分：题目、网址提取、只认看到过的网址、汇总口径、工具名单。"""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from bench import web_eval as we
from flywhl_harness.events import E


def test_ten_unique_questions():
    qs = we.load_questions()
    assert len(qs) == 10 and len({q["id"] for q in qs}) == 10 and all(q["text"].strip() for q in qs)


def test_answer_urls_strip_trailing_punctuation_and_cjk():
    text = "见 https://www.python.org/downloads/。另见（https://docs.python.org/3/whatsnew/3.14.html）和 http://a.example/x."
    assert we.answer_urls(text) == ["https://www.python.org/downloads/", "https://docs.python.org/3/whatsnew/3.14.html",
                                    "http://a.example/x"]


def test_only_urls_the_model_saw_count():
    evs = [NS(type=E.TOOL_END, data={"details": {"hits": [{"url": "https://a.example/"}]}}),
           NS(type=E.TOOL_END, data={"details": {"url": "https://b.example/p", "final_url": "https://c.example/p"}}),
           NS(type=E.TOOL_START, data={"details": {"hits": [{"url": "https://ignored.example/"}]}})]
    assert we.seen_urls(evs) == {"https://a.example/", "https://b.example/p", "https://c.example/p"}


def test_summary():
    rows = [{"status": "completed", "searches": 2, "fetches": 1, "model_usd": 0.002, "tool_usd": 0.00225, "openable": ["u"]},
            {"status": "completed", "searches": 0, "fetches": 0, "model_usd": 0.001, "tool_usd": 0.0, "openable": []}]
    s = we.summarize(rows)
    assert s["with_openable_source"] == 0.5 and s["avg_searches"] == 1.0 and s["avg_cost_usd"] == pytest.approx(0.002625)


def test_only_web_tools_are_given(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-real")
    h = we.build("search", "deepseek:deepseek-flash", tmp_path)
    assert {t.name for t in h.tools} == we.WEB_TOOLS
    assert h.config.budget.max_cost_usd == 0.05   # 每题有费用上限
    assert we.build("search", "deepseek:deepseek-flash", tmp_path, 0.2).config.budget.max_cost_usd == 0.2
