"""工具注册表。

两个容易被忽视的细节：
1. **顺序稳定**：工具规格是请求前缀的一部分，顺序一变前缀缓存就失效（Codex 曾因 MCP 工具
   枚举顺序不稳定导致缓存命中下降）。这里按注册顺序输出；会话创建时把工具名单钉进会话元数据，
   之后注册表再增删工具，也不会改变已有会话的工具前缀（见 ``Harness._build_rc``）。
2. **按需裁剪而非删除**：plan 模式、子代理等场景需要“禁用”部分工具时，优先在执行层拒绝
   （Manus：mask 而非 remove），只有在全新上下文（如子代理）里才真正裁剪工具集。
"""

from __future__ import annotations

from typing import Iterable

from .base import Tool


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()):
        self._tools: dict[str, Tool] = {}
        for t in tools:
            self.register(t)

    def register(self, tool: Tool, *, replace: bool = False) -> Tool:
        if not tool.name:
            raise ValueError("工具必须有名字")
        if tool.name in self._tools and not replace:
            raise ValueError(f"工具重名：{tool.name}")
        self._tools[tool.name] = tool
        return tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __iter__(self):
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)

    def specs(self) -> list[dict]:
        return [t.spec() for t in self._tools.values()]

    def subset(self, names: Iterable[str] | None = None, exclude: Iterable[str] = ()) -> "ToolRegistry":
        """派生子集（用于子代理：更窄的工具集 = 更小的爆炸半径）。"""
        excl = set(exclude)
        keep = list(self._tools) if names is None else [n for n in names if n in self._tools]
        return ToolRegistry(self._tools[n] for n in keep if n not in excl)
