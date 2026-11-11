"""联网的 HTTP 接口：建会话时 clamp、PATCH 只能收紧、跨源写被拒；/api/info 与设置页不含密钥和端点。"""

from __future__ import annotations

import json

import httpx

from flywhl_harness.server.app import create_app

from .conftest import make_harness
from .webkit import DS_FETCH, DS_SEARCH, FAKE_KEY, Net, ds_search, fixture, make_web_harness

ORIGIN = "http://127.0.0.1:8765"


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN)


def web_net() -> Net:
    return Net().on("POST", DS_SEARCH, ds_search()).on("POST", DS_FETCH, httpx.Response(200, json=fixture("deepseek", "fetch")))


async def test_info_and_settings_expose_levels_but_no_secrets(ws):
    h = make_web_harness(ws, [], web_net(), web__default="search",
                         web__backend={"deepseek": {"api_key_env": "DEEPSEEK_API_KEY", "endpoint": "https://api.deepseek.com"}})
    async with _client(create_app(h, auto_recover=False)) as c:
        info = (await c.get("/api/info")).json()
        settings = (await c.get("/api/settings")).json()
    web = info["capabilities"]["web"]
    assert web == {"available": True, "fetch_available": True, "backends": ["deepseek"], "default": "search", "reason": "",
                   "via": "tools", "native": None, "limits": {"searches": 20, "fetches": 10}}
    assert settings["web"]["level"] == "fetch" and settings["web"]["backends"][0] == {
        "name": "deepseek", "region": "境内", "available": True, "reason": "", "priced": True}
    blob = json.dumps([info, settings], ensure_ascii=False)
    for secret in (FAKE_KEY, "api.deepseek.com", "DEEPSEEK_API_KEY", "api_key_env"):
        assert secret not in blob, secret


async def test_disabled_web_keeps_settings_and_session_shape(ws):
    h = make_harness(ws, [])
    async with _client(create_app(h, auto_recover=False)) as c:
        info = (await c.get("/api/info")).json()
        settings = (await c.get("/api/settings")).json()
        plain = (await c.post("/api/sessions", json={"title": "x"})).json()
        asked = (await c.post("/api/sessions", json={"title": "y", "capabilities": {"web": "search"}})).json()
    assert info["capabilities"]["web"]["available"] is False and "enabled = false" in info["capabilities"]["web"]["reason"]
    assert "web" not in settings
    assert "capabilities" not in plain and "capabilities" not in plain["meta"]
    assert asked["capabilities"] == {"web": "off", "requested": "search",
                                     "reason": "联网工具没有启用（用户配置 [web] enabled = false）"}


async def test_new_session_is_clamped_and_patch_only_tightens(ws):
    # 博查能搜，读取用的 DeepSeek 没有密钥：上限是 search
    h = make_web_harness(ws, [], web_net(), env={"BOCHA_API_KEY": FAKE_KEY}, web__backends=["bocha"], web__default="off")
    async with _client(create_app(h, auto_recover=False)) as c:
        assert (await c.post("/api/sessions", json={"capabilities": {"web": "everything"}})).status_code == 400
        s = (await c.post("/api/sessions", json={"capabilities": {"web": "fetch"}})).json()
        assert s["capabilities"]["web"] == "search" and s["capabilities"]["requested"] == "fetch"
        assert "网页读取不可用" in s["capabilities"]["reason"]
        assert "web_fetch" not in s["meta"]["tools"] and "web_search" in s["meta"]["tools"]
        sid = s["id"]
        assert (await c.patch(f"/api/sessions/{sid}/capabilities", json={"web": "fetch"})).status_code == 409
        r = await c.patch(f"/api/sessions/{sid}/capabilities", json={"web": "off"})
        assert r.status_code == 200 and r.json()["web"] == "off"
        again = await c.patch(f"/api/sessions/{sid}/capabilities", json={"web": "search"})
        assert again.status_code == 409 and "只能收紧" in again.json()["detail"]
        assert (await c.patch(f"/api/sessions/{sid}/capabilities", json={"web": "loud"})).status_code == 400
        assert (await c.patch("/api/sessions/s_nope/capabilities", json={"web": "off"})).status_code == 404
        default = (await c.post("/api/sessions", json={})).json()
        assert default["capabilities"]["web"] == "off"
    assert h.store.get_session(sid).meta["capabilities"]["web"] == "off"


async def test_cross_origin_patch_is_refused(ws):
    h = make_web_harness(ws, [], web_net(), web__default="search")
    sid = h.create_session("x").id
    async with _client(create_app(h, auto_recover=False)) as c:
        r = await c.patch(f"/api/sessions/{sid}/capabilities", json={"web": "off"}, headers={"Origin": "https://evil.example"})
        assert r.status_code == 403
        r = await c.post("/api/sessions", json={"capabilities": {"web": "search"}}, headers={"Origin": "null"})
        assert r.status_code == 403
    assert h.store.get_session(sid).meta["capabilities"]["web"] == "search"


async def test_context_inspector_counts_the_tools_the_session_actually_sends(ws):
    h = make_web_harness(ws, [], web_net(), web__default="fetch")
    on = h.create_session("on").id
    off = h.create_session("off", capabilities=h.web_capability("off")).id
    async with _client(create_app(h, auto_recover=False)) as c:
        tokens = lambda sid: c.get(f"/api/sessions/{sid}/context")   # noqa: E731
        with_web = (await tokens(on)).json()["tools_tokens"]
        h.set_capabilities(on, "search")
        masked = (await tokens(on)).json()["tools_tokens"]
        without = (await tokens(off)).json()["tools_tokens"]
    assert masked == with_web > without     # 收紧只是屏蔽：规格照样发送，token 照样算


async def test_native_search_shows_up_in_settings(ws):
    h = make_harness(ws, [], extra_body={"enable_search": True})
    async with _client(create_app(h, auto_recover=False)) as c:
        web = (await c.get("/api/settings")).json()["web"]
    assert web["enabled"] is False and web["native_via_extra_body"] is True and web["native_keys"] == ["enable_search"]
