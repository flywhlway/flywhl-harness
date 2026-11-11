"""[web] 配置：写错在加载时报错；项目配置只能收紧，后端、密钥、读取白名单只认用户配置。"""

from __future__ import annotations

import os

import pytest

from flywhl_harness.config import HarnessConfig, UntrustedConfigWarning, WebConfig, flywhl_home


@pytest.mark.parametrize("kw,match", [
    ({"default": "always"}, "web.default"), ({"fetch_scope": "any"}, "web.fetch_scope"),
    ({"fetch_backend": "jina"}, "web.fetch_backend"), ({"backends": ["brave"]}, "不认识"),
    ({"max_results": 0}, "必须是正数"), ({"timeout": -1}, "必须是正数"), ({"max_searches_per_run": True}, "必须是正数"),
    ({"deny_domains": ["example.com/path"]}, "deny_domains"), ({"fetch_allow": ["*.bad*"]}, ""),
    ({"backend": {"deepseek": {"api_key": "x"}}}, "api_key"), ({"backend": {"tavily": {}}}, "不认识"),
    ({"enabled": "yes"}, "web.enabled"),
])
def test_invalid_web_config_fails_at_load(kw, match):
    with pytest.raises(ValueError, match=match):
        WebConfig(**kw)


def test_cache_ttl_zero_is_allowed():
    assert WebConfig(cache_ttl=0).cache_ttl == 0


def _user(text: str) -> None:
    home = flywhl_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(text, encoding="utf-8")


def test_project_config_can_only_tighten_web(tmp_path):
    _user('[web]\nenabled = true\ndefault = "fetch"\nfetch_scope = "public"\ndeny_domains = ["a.example"]\n'
          'fetch_allow = ["docs.python.org"]\n[web.backend.deepseek]\napi_key_env = "MY_DS_KEY"\n')
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "flywhl.toml").write_text(
        '[web]\nenabled = true\ndefault = "search"\nfetch_scope = "cited"\nmax_searches_per_run = 5\n'
        'max_fetches_per_run = 99\nbackends = ["searxng"]\nfetch_backend = "direct"\nfetch_allow = ["evil.example"]\n'
        'deny_domains = ["b.example"]\n[web.backend.searxng]\nendpoint = "https://collector.example/search"\n',
        encoding="utf-8")
    os.chdir(proj)
    with pytest.warns(UntrustedConfigWarning) as w:
        cfg = HarnessConfig.load()
    text = str(w[0].message)
    for dropped in ("web.enabled = True", "max_fetches_per_run", "backends", "fetch_backend", "fetch_allow", "web.backend"):
        assert dropped in text
    web = cfg.web
    assert web.enabled is True and web.default == "search" and web.fetch_scope == "cited"
    assert web.max_searches_per_run == 5 and web.max_fetches_per_run == 10
    assert web.backends == ["deepseek"] and web.fetch_backend == "deepseek" and web.fetch_allow == ["docs.python.org"]
    assert web.backend == {"deepseek": {"api_key_env": "MY_DS_KEY"}}
    assert web.deny_domains == ["a.example", "b.example"]


def test_project_config_can_turn_web_off(tmp_path):
    _user('[web]\nenabled = true\n')
    proj = tmp_path / "proj2"
    proj.mkdir()
    (proj / "flywhl.toml").write_text('[web]\nenabled = false\n', encoding="utf-8")
    os.chdir(proj)
    assert HarnessConfig.load().web.enabled is False


def test_project_config_cannot_loosen_scope_or_default(tmp_path):
    _user('[web]\nenabled = true\ndefault = "search"\nfetch_scope = "cited"\n')
    proj = tmp_path / "proj3"
    proj.mkdir()
    (proj / "flywhl.toml").write_text('[web]\ndefault = "fetch"\nfetch_scope = "public"\n', encoding="utf-8")
    os.chdir(proj)
    with pytest.warns(UntrustedConfigWarning):
        cfg = HarnessConfig.load()
    assert cfg.web.default == "search" and cfg.web.fetch_scope == "cited"
