"""模型侧联网：声明决定开不开；Anthropic server tool 块的解析、回传与计数；会话二选一、只能收紧；回放一致。

全部离线：Anthropic 的流事件用 SimpleNamespace 构造，Harness 层用脚本 provider 冒充 Anthropic 官方端点。
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace as NS

import pytest

from flywhl_harness import Harness, ScriptedProvider, Store
from flywhl_harness.cancel import CancelToken
from flywhl_harness.config import HarnessConfig, UntrustedConfigWarning, WebConfig
from flywhl_harness.llm import native as nat
from flywhl_harness.llm.base import ModelRequest, StreamEvent
from flywhl_harness.llm.native import NATIVE_SPECS, native_for, summarize
from flywhl_harness.llm.openai_compat import OpenAICompatProvider
from flywhl_harness.llm.replay import context_fingerprint
from flywhl_harness.llm.resilient import ResilientProvider
from flywhl_harness.observe.replay import replay_run
from flywhl_harness.types import Message, ServerBlock, TextBlock

anthropic_sdk = pytest.importorskip("anthropic")
from flywhl_harness.llm.anthropic import AnthropicProvider, to_anthropic_messages  # noqa: E402

HOST = "api.anthropic.com"
URL = "https://news.example/a"


@pytest.fixture(autouse=True)
def _no_base_url(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)


def _req(**kw) -> ModelRequest:
    return ModelRequest(model=kw.pop("model", "claude-sonnet-5-5"), system="s",
                        messages=[Message(role="user", content=[TextBlock("q")])], **kw)


# ====================================================================== 声明与门槛
def test_only_confirmed_vendors_and_models_are_enabled():
    spec, why = native_for(AnthropicProvider(api_key="k"), "claude-sonnet-5-5")
    assert spec is NATIVE_SPECS["anthropic"] and why == ""
    spec, why = native_for(AnthropicProvider(api_key="k"), "gpt-x")
    assert spec is None and "已确认的模型" in why
    # 同一协议、不同端点：DeepSeek 的 Anthropic 兼容端点没确认能和函数工具一起用
    spec, why = native_for(AnthropicProvider(api_key="k", base_url="https://api.deepseek.com/anthropic"), "deepseek-v4-pro")
    assert spec is None and "函数工具" in why
    spec, why = native_for(OpenAICompatProvider(api_key="k", profile="dashscope"), "qwen3.8-flash")
    assert spec is None and "函数工具" in why
    spec, why = native_for(ScriptedProvider([]), "scripted")
    assert spec is None and "没有模型侧联网的声明" in why


def test_fallback_chain_is_judged_by_its_primary():
    chain = ResilientProvider([(AnthropicProvider(api_key="k"), "claude-sonnet-5-5"), (ScriptedProvider([]), "m")])
    assert native_for(chain, "ignored")[0] is NATIVE_SPECS["anthropic"]
    chain = ResilientProvider([(ScriptedProvider([]), "m"), (AnthropicProvider(api_key="k"), "claude-sonnet-5-5")])
    assert native_for(chain, "m")[0] is None


# ====================================================================== 请求
def test_server_tool_is_added_only_when_the_request_asks_and_the_endpoint_matches():
    off = AnthropicProvider(api_key="k")._kwargs(_req())[0]
    assert "tools" not in off
    kw = AnthropicProvider(api_key="k")._kwargs(_req(native_search="anthropic",
                                                     native_opts={"max_uses": 2, "deny_domains": [" .Bad.example "]}))[0]
    assert kw["tools"] == [{"type": "web_search_20250305", "name": "web_search", "max_uses": 2,
                            "blocked_domains": ["bad.example"]}]
    # 剩余次数多于声明上限时取声明的
    kw = AnthropicProvider(api_key="k")._kwargs(_req(native_search="anthropic", native_opts={"max_uses": 50}))[0]
    assert kw["tools"][-1]["max_uses"] == 5
    # 降级到别的端点：声明对不上，不带
    gw = AnthropicProvider(api_key="k", base_url="https://openrouter.ai/api")
    assert "tools" not in gw._kwargs(_req(native_search="anthropic"))[0]
    # 函数工具在前，server tool 追加在后
    fn = {"name": "read", "description": "d", "parameters": {"type": "object"}}
    kw = AnthropicProvider(api_key="k")._kwargs(_req(tools=[fn], native_search="anthropic"))[0]
    assert [t["name"] for t in kw["tools"]] == ["read", "web_search"]


def test_openai_compat_merges_declared_fields_only_on_match():
    p = OpenAICompatProvider(api_key="k", profile="dashscope")
    body = p._payload(_req(model="qwen3.8-flash", native_search="dashscope"))
    assert body["enable_search"] is True and body["search_options"] == {"search_strategy": "turbo"}
    assert "enable_search" not in p._payload(_req(model="qwen3.8-flash"))
    assert "enable_search" not in p._payload(_req(model="qwen3.8-flash", native_search="anthropic"))
    other = OpenAICompatProvider(api_key="k", profile="deepseek")
    assert "enable_search" not in other._payload(_req(model="qwen3.8-flash", native_search="dashscope"))


def test_fingerprint_unchanged_without_native_search():
    a, b = _req(), _req(native_search="anthropic")
    assert context_fingerprint(a) == context_fingerprint(_req())
    assert context_fingerprint(a) != context_fingerprint(b)


# ====================================================================== 流解析
class _Stream:
    def __init__(self, events):
        self.events = events

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for e in self.events:
            yield e

    async def close(self):
        pass


class _Messages:
    def __init__(self, events):
        self.events, self.kw = events, None

    async def create(self, **kw):
        self.kw = kw
        return _Stream(self.events)


def _events(*, result_content, usage_stu) -> list:
    u = NS(input_tokens=10, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    return [
        NS(type="message_start", message=NS(usage=u, model="claude-sonnet-5-5")),
        NS(type="content_block_start", index=0, content_block=NS(type="text", text="我查一下。")),
        NS(type="content_block_stop", index=0),
        NS(type="content_block_start", index=1, content_block=NS(type="server_tool_use", id="srvtoolu_1", name="web_search", input={})),
        NS(type="content_block_delta", index=1, delta=NS(type="input_json_delta", partial_json='{"query": "今日')),
        NS(type="content_block_delta", index=1, delta=NS(type="input_json_delta", partial_json='新闻"}')),
        NS(type="content_block_stop", index=1),
        NS(type="content_block_start", index=2, content_block=NS(type="web_search_tool_result", tool_use_id="srvtoolu_1",
                                                                  content=result_content)),
        NS(type="content_block_stop", index=2),
        NS(type="content_block_start", index=3, content_block=NS(type="text", text="")),
        NS(type="content_block_delta", index=3, delta=NS(type="citations_delta", citation=NS(
            type="web_search_result_location", url=URL, title="A", cited_text="…", encrypted_index="e1"))),
        NS(type="content_block_delta", index=3, delta=NS(type="text_delta", text="答案见来源 [1]。")),
        NS(type="content_block_stop", index=3),
        NS(type="message_delta", delta=NS(stop_reason="end_turn"), usage=NS(output_tokens=20, server_tool_use=usage_stu)),
    ]


async def _stream(events, req) -> tuple[list[StreamEvent], Message, dict]:
    p = AnthropicProvider(api_key="k", server_fallback=False)
    msgs = _Messages(events)
    p.client = NS(messages=msgs)
    evs = [ev async for ev in p.stream(req, CancelToken())]
    return evs, evs[-1].message, msgs.kw


async def test_server_blocks_are_kept_and_summarized():
    hits = [NS(type="web_search_result", url=URL, title="A", encrypted_content="enc", page_age="1 day"),
            NS(type="web_search_result", url=URL, title="A dup", encrypted_content="enc2", page_age=None),
            NS(type="web_search_result", url="https://b.example/", title="B", encrypted_content="enc3", page_age=None)]
    evs, msg, kw = await _stream(_events(result_content=hits, usage_stu=NS(web_search_requests=1)),
                                 _req(native_search="anthropic", native_opts={"max_uses": 3}))
    assert kw["tools"][-1]["max_uses"] == 3
    # server_tool_use 的参数增量不冒充函数工具调用
    assert not [e for e in evs if e.type.startswith("toolcall")] and msg.tool_calls == []
    servers = [c for c in msg.content if isinstance(c, ServerBlock)]
    assert [s.data["type"] for s in servers] == ["server_tool_use", "web_search_tool_result"]
    assert servers[0].data["input"] == {"query": "今日新闻"} and all(s.origin == HOST for s in servers)
    assert servers[1].data["content"][0]["encrypted_content"] == "enc"   # 原样保存，续写时回传
    nw = msg.meta["native_web"]
    assert nw["spec"] == "anthropic" and nw["searches"] == 1 and nw["queries"] == ["今日新闻"] and nw["errors"] == []
    assert [(s["n"], s["url"], s["cited"]) for s in nw["sources"]] == [(1, URL, True), (2, "https://b.example/", False)]
    assert msg.text == "我查一下。答案见来源 [1]。"


async def test_search_error_object_is_recorded_and_count_falls_back_to_blocks():
    _, msg, _ = await _stream(_events(result_content=NS(type="web_search_tool_result_error", error_code="max_uses_exceeded"),
                                      usage_stu=None), _req(native_search="anthropic"))
    nw = msg.meta["native_web"]
    assert nw["errors"] == ["max_uses_exceeded"] and nw["sources"] == [] and nw["searches"] == 1


async def test_no_summary_when_native_search_was_not_requested():
    _, msg, _ = await _stream(_events(result_content=[], usage_stu=NS(web_search_requests=0)), _req())
    assert "native_web" not in msg.meta


def test_summarize_takes_the_larger_usage_counter():
    spec = NATIVE_SPECS["deepseek-anthropic"]
    assert summarize(spec, [], {"web_search_requests": 1, "tool_search": 2}, [])["searches"] == 2
    assert summarize(spec, [], {}, []) is None


# ====================================================================== 回传
def _assistant(origin: str = HOST, provider: str = "anthropic", *, result: bool = True) -> Message:
    blocks = [TextBlock("先查"), ServerBlock({"type": "server_tool_use", "id": "s1", "name": "web_search",
                                              "input": {"query": "q"}}, origin)]
    if result:
        blocks.append(ServerBlock({"type": "web_search_tool_result", "tool_use_id": "s1", "content": []}, origin))
    blocks.append(TextBlock("答"))
    return Message(role="assistant", content=blocks, provider=provider, stop_reason="end_turn")


def test_server_blocks_round_trip_and_echo_only_to_the_same_endpoint():
    m = Message.from_dict(_assistant().to_dict())
    assert [type(c).__name__ for c in m.content] == ["TextBlock", "ServerBlock", "ServerBlock", "TextBlock"]
    assert m.content[1].origin == HOST
    user = Message(role="user", content=[TextBlock("q")])
    out = to_anthropic_messages([user, m], HOST)[1]["content"]
    assert [b["type"] for b in out] == ["text", "server_tool_use", "web_search_tool_result", "text"]
    for msgs, host in (([user, m], "api.deepseek.com"), ([user, _assistant(result=False)], HOST),
                       ([user, _assistant(provider="openai")], HOST)):
        assert [b["type"] for b in to_anthropic_messages(msgs, host)[1]["content"]] == ["text", "text"]


def test_other_providers_ignore_server_blocks():
    from flywhl_harness.llm.openai_compat import to_openai_messages

    out = to_openai_messages("s", [Message(role="user", content=[TextBlock("q")]), _assistant()])
    assert out[-1]["content"] == "先查答"


# ====================================================================== 配置
def test_native_flag_is_a_bool_and_project_config_cannot_turn_it_on(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="web.native"):
        WebConfig(native="yes")
    from flywhl_harness.config import flywhl_home

    home = flywhl_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text("[web]\nenabled = true\n", encoding="utf-8")
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "flywhl.toml").write_text("[web]\nnative = true\n", encoding="utf-8")
    monkeypatch.chdir(proj)
    with pytest.warns(UntrustedConfigWarning, match="web.native"):
        assert HarnessConfig.load().web.native is False


# ====================================================================== Harness
class FakeClaude(ScriptedProvider):
    """冒充 Anthropic 官方端点：请求带了模型侧联网、脚本这一步写了 ``native`` 时，往回复里放一次搜索。"""

    name = "anthropic"
    base_url = None

    def __init__(self, script, natives: dict[int, dict] | None = None, notices: dict[int, dict] | None = None):
        super().__init__(script)
        self.natives, self.notices = natives or {}, notices or {}

    async def stream(self, req, cancel):
        i = len(self.requests)
        if i in self.notices:
            yield StreamEvent("notice", info=self.notices[i])
        async for ev in super().stream(req, cancel):
            if ev.type == "done" and req.native_search and i in self.natives:
                nw = {"spec": req.native_search, "queries": [], "sources": [], "errors": [], **self.natives[i]}
                ev.message.meta["native_web"] = nw
                ev.message.content.insert(0, ServerBlock({"type": "server_tool_use", "id": f"s{i}", "name": "web_search",
                                                          "input": {"query": "q"}}, HOST))
                ev.message.content.insert(1, ServerBlock({"type": "web_search_tool_result", "tool_use_id": f"s{i}",
                                                          "content": []}, HOST))
            yield ev


ONE = {"searches": 1, "sources": [{"n": 1, "title": "A", "url": URL, "page_age": None, "cited": True}]}


def _harness(ws, provider, *, native=True, store=None, **over) -> Harness:
    cfg = HarnessConfig.load(workspace=str(ws))
    cfg.permissions.on_ask = "allow"
    cfg.snapshots = False
    cfg.heartbeat_seconds = 0.2
    cfg.trust_workspace = True
    cfg.web.enabled, cfg.web.default, cfg.web.native = True, "search", native
    for k, v in over.items():
        setattr(cfg.web, k, v)
    return Harness(cfg, provider=provider, model="claude-sonnet-5-5", store=store or Store(":memory:"))


def test_session_picks_native_and_pins_no_web_tools(ws):
    h = _harness(ws, FakeClaude(["好"]))
    s = h.create_session("t")
    cap = s.meta["capabilities"]
    assert cap == {"web": "search", "requested": "search", "reason": "", "via": "native", "native": "anthropic"}
    assert not {"web_search", "web_fetch"} & set(s.meta["tools"]) and not s.meta.get("tainted")
    # 请求 fetch 时降到 search，并说明原因
    cap = h.web_capability("fetch")
    assert cap["web"] == "search" and "只能搜索" in cap["reason"]
    # 没开 native：照常用内置工具
    plain = _harness(ws, FakeClaude(["好"]), native=False).create_session("t")
    assert "via" not in plain.meta.get("capabilities", {})


def test_unobservable_declaration_taints_at_creation(ws, monkeypatch):
    monkeypatch.setitem(NATIVE_SPECS, "anthropic", replace(NATIVE_SPECS["anthropic"], observable=False))
    assert _harness(ws, FakeClaude(["好"])).create_session("t").meta["tainted"] is True


async def test_native_searches_are_counted_priced_and_taint(ws):
    p = FakeClaude([{"tool_calls": [{"id": "c1", "name": "ls", "args": {}}]}, "好"], natives={0: ONE})
    h = _harness(ws, p, max_searches_per_run=3)
    res = await h.run("今天有什么新闻")
    run = h.store.get_run(res.run_id)
    assert run.result["web_searches"] == 1 and run.result["tool_cost_usd"] == 0.01
    assert run.checkpoint["budget"]["web_searches"] == 1
    assert [r.native_search for r in p.requests] == ["anthropic", "anthropic"]
    assert [r.native_opts["max_uses"] for r in p.requests] == [3, 2]   # 剩余次数随计数减少
    assert h.store.get_session(res.session_id).meta["tainted"] is True
    assert any(e.type == "notice" and e.data.get("code") == "tainted" for e in res.events)


async def test_no_native_search_once_the_run_budget_is_used(ws):
    p = FakeClaude([{"tool_calls": [{"id": "c1", "name": "ls", "args": {}}]}, "好"], natives={0: ONE})
    h = _harness(ws, p, max_searches_per_run=1)
    await h.run("x")
    assert [r.native_search for r in p.requests] == ["anthropic", None]


async def test_tightening_to_off_stops_native_search(ws):
    p = FakeClaude(["一", "二"])
    h = _harness(ws, p)
    s = h.create_session("t")
    await h.run("x", session_id=s.id)
    h.set_capabilities(s.id, "off")
    await h.run("y", session_id=s.id)
    assert [r.native_search for r in p.requests] == ["anthropic", None]
    with pytest.raises(PermissionError):
        h.set_capabilities(s.id, "search")


async def test_fallback_announces_lost_native_search(ws):
    p = FakeClaude(["好"], notices={0: {"code": "fallback", "text": "降级到 x/y"}})
    res = await _harness(ws, p).run("x")
    assert any(e.type == "notice" and e.data.get("code") == "native_web_lost" for e in res.events)


async def test_off_session_requests_are_unchanged(ws):
    p = FakeClaude(["好"])
    h = _harness(ws, p, native=False)
    h.config.web.default = "off"
    await h.run("x")
    assert p.requests[0].native_search is None and p.requests[0].native_opts == {}


async def test_native_session_replays_deterministically(ws):
    p = FakeClaude([{"tool_calls": [{"id": "c1", "name": "ls", "args": {}}]}, "好"], natives={0: ONE})
    h = _harness(ws, p)
    res = await h.run("x")
    rep = await replay_run(h.store, res.run_id, config=h.config, base_tools=h.tools)
    # 录制的请求指纹里有 native_search：回放的请求也带上了才对得上
    assert rep.divergence is None, rep.notes
    assert rep.same_tool_sequence and rep.same_final_text and rep.deterministic


def test_module_exports():
    assert set(nat.__all__) >= {"native_for", "summarize", "server_tools"}


# ====================================================================== 可观测与压缩
async def test_chat_span_carries_native_search_counts_without_queries(ws):
    from flywhl_harness.observe.trace import export_trajectory

    p = FakeClaude([{"tool_calls": [{"id": "c1", "name": "ls", "args": {}}]}, "好"],
                   natives={0: {**ONE, "queries": ["秘密查询词"]}})
    h = _harness(ws, p)
    res = await h.run("x")
    spans = export_trajectory(h.store, res.run_id)["spans"]
    chat = [s for s in spans if s["attributes"].get("gen_ai.operation.name") == "chat"]
    assert chat[0]["attributes"]["flywhl.web.backend"] == "native:anthropic"
    assert chat[0]["attributes"]["flywhl.web.searches"] == 1 and chat[0]["attributes"]["flywhl.web.hits"] == 1
    assert "flywhl.web.searches" not in chat[1]["attributes"]
    assert "秘密查询词" not in str(spans)


def test_tool_span_web_attrs():
    from flywhl_harness.observe.trace import _web_attrs

    search = {"backend": "bocha", "query": "q", "hits": [{"n": 1}, {"n": 2}], "cached": True, "web": {"op": "search"}}
    assert _web_attrs(search) == {"flywhl.web.backend": "bocha", "flywhl.web.hits": 2, "flywhl.web.cached": True}
    fetch = {"backend": "direct", "url": "https://a.example/p?k=v", "final_url": "https://b.example/p?k=v", "status": 200,
             "web": {"op": "fetch"}}
    assert _web_attrs(fetch) == {"flywhl.web.backend": "direct", "server.address": "b.example", "http.response.status_code": 200}
    assert _web_attrs({"status": "ok"}) == {}   # 不是联网工具


def _long_history(h, sid: str) -> None:
    for i in range(6):
        h.store.append_entry(sid, "message", Message(role="user", content=[TextBlock(f"第 {i} 段 " + "资料" * 3000)]).to_dict())
        h.store.append_entry(sid, "message", Message(role="assistant", content=[TextBlock("收到" * 2000)],
                                                     stop_reason="end_turn", provider="anthropic").to_dict())


@pytest.mark.parametrize("level,expect", [("search", True), ("off", False)])
async def test_compaction_keeps_sources_only_in_web_sessions(ws, level, expect):
    p = FakeClaude(["好"])
    h = _harness(ws, p, native=False)
    h.config.web.default = level
    s = h.create_session("t")
    _long_history(h, s.id)
    assert await h.compact(s.id) is not None
    prompt = next(r for r in p.requests if r.purpose == "compaction").messages[-1].text
    assert ("已查阅来源" in prompt) is expect


async def test_api_describes_native_search_without_secrets(ws):
    from flywhl_harness.server.app import create_app

    from .test_serve_web import _client

    h = _harness(ws, FakeClaude([]))
    async with _client(create_app(h, auto_recover=False)) as c:
        info = (await c.get("/api/info")).json()["capabilities"]["web"]
        settings = (await c.get("/api/settings")).json()["web"]
    assert info["via"] == "native" and info["backends"] == [] and not info["fetch_available"]
    assert info["native"]["name"] == "anthropic" and info["native"]["observable"] is True
    assert settings["native"]["configured"] is True and settings["native"]["active"]["price_per_call"] == 0.01
    off = _harness(ws, FakeClaude([]), native=False)
    async with _client(create_app(off, auto_recover=False)) as c:
        settings = (await c.get("/api/settings")).json()["web"]
    assert settings["native"] == {"configured": False, "active": None, "reason": ""}
