"""运行中的用户输入：steering（插话）与 follow-up（排队）。

用户在 Agent 工作时继续输入，有两种语义（借鉴 pi）：

- **steer（插话）**：在 *下一个安全点*（当前这批工具全部执行完、下一次模型调用之前）注入。
  不会打断正在执行的工具，也不会破坏 tool_call / tool_result 的配对；
- **follow-up（排队）**：只在 Agent *本来要停下* 时才注入，相当于“做完这个再做那个”。

两个队列都支持 ``one_at_a_time``（每次取一条）与 ``all``（一次取完）。
真正的“停止”走取消语义（CancelToken），与插话是两回事。
"""

from __future__ import annotations

from collections import deque


class Inbox:
    def __init__(self, mode: str = "all"):
        self.mode = mode
        self._steer: deque[str] = deque()
        self._follow: deque[str] = deque()

    def steer(self, text: str) -> None:
        self._steer.append(text)

    def follow_up(self, text: str) -> None:
        self._follow.append(text)

    def _drain(self, q: deque[str]) -> list[str]:
        if not q:
            return []
        if self.mode == "one_at_a_time":
            return [q.popleft()]
        items = list(q)
        q.clear()
        return items

    def drain_steer(self) -> list[str]:
        return self._drain(self._steer)

    def drain_follow_up(self) -> list[str]:
        return self._drain(self._follow)

    def pending(self) -> dict[str, list[str]]:
        return {"steer": list(self._steer), "follow_up": list(self._follow)}

    def clear(self) -> None:
        self._steer.clear()
        self._follow.clear()
