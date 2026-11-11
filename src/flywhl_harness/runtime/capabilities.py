"""会话能力位：``session.meta["capabilities"] = {"web": off|search|fetch, "requested": ..., "reason": ...}``。

- 建会话时定：请求值 clamp 到用户配置与可用后端允许的上限，按结果决定是否把 ``web_search`` / ``web_fetch`` 钉进工具名单；
- 开工后只能收紧：工具名单不变（前缀缓存），执行层在权限判定之前按当前值拒绝（mask，不是 remove）；
- 子代理的会话挂在父会话下面，生效值取整条链上最严的一个：父会话收紧，正在跑的子代理下一次调用就受约束；
- 老会话没有这个字段，按 ``off`` 处理；
- 用模型侧联网的会话另记 ``"via": "native", "native": <llm/native.py 里的声明名>``，同样只能收紧。
"""

from __future__ import annotations

from typing import Any

from ..config import WEB_LEVELS, web_rank

_MAX_CHAIN = 16


def strictest(*levels: str) -> str:
    return min(levels, key=web_rank) if levels else "off"


def clamp(requested: str | None, ceiling: str, ceiling_reason: str = "") -> tuple[str, str]:
    """(生效值, 原因)。原因只在被降级时给出。"""
    req = requested if requested in WEB_LEVELS else "off"
    if web_rank(req) <= web_rank(ceiling):
        return req, ""
    return ceiling, ceiling_reason or f"上限是 {ceiling}"


def session_web(meta: dict[str, Any] | None) -> str:
    cap = (meta or {}).get("capabilities") or {}
    level = cap.get("web") if isinstance(cap, dict) else None
    return level if level in WEB_LEVELS else "off"


def session_native(cap: dict[str, Any] | None) -> Any:
    """会话选的模型侧联网声明（``NativeSearchSpec``）；没选、已收紧到 off、或声明已不存在时返回 None。"""
    from ..llm.native import NATIVE_SPECS

    if not isinstance(cap, dict) or cap.get("via") != "native" or session_web({"capabilities": cap}) == "off":
        return None
    return NATIVE_SPECS.get(cap.get("native") or "")


def current_web(store: Any, session_id: str) -> str:
    """沿父会话链取最严的生效值。每次联网调用前都从存储读：别的 Worker 改的也算数。"""
    level, sid, n = "fetch", session_id, 0
    while sid and n < _MAX_CHAIN:
        s = store.get_session(sid)
        if s is None:
            return "off"
        level = strictest(level, session_web(s.meta))
        sid, n = s.parent_session_id, n + 1
    return level


__all__ = ["clamp", "current_web", "session_native", "session_web", "strictest"]
