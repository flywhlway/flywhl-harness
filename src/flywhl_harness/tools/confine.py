"""把子进程关进系统沙箱：bash、命令 hook、MCP 服务器、验收命令共用这一层。

``tools/sandbox.py`` 只生成沙箱的启动参数，这里管运行时的事：把配置解析成实际生效的档位，起出站代理，给 hook 和 MCP 服务器
各分一份可写目录。四条通道走同一段代码，才不会出现“bash 进了沙箱，改了配置文件的 hook 还在外面”这种缺口。

解析规则
--------
- ``sandbox = "auto"``（默认）：``detect()`` 探测到什么用什么；探测不到时 **不退回沙箱外**，``enter()`` 拒绝执行并给出修法。
  只有明确写了 ``off`` 才在沙箱外运行，而且项目配置写的不算（见 ``config.py``）；
- ``sandbox_network = "auto"``（默认）：有沙箱时是空白名单的 ``allowlist``，效果等于断网，但代理知道被拦的是哪个主机，
  报错能直接写出加白名单的办法；明确 ``off`` 时是 ``allow``，已有配置的行为不变；
- 明确写了 ``deny`` / ``allowlist`` 却没有沙箱时拒绝执行，不悄悄连外网。

独立目录
--------
hook 与 MCP 服务器是长期或反复运行的第三方代码，用 ``scoped()`` 各取一份：Linux 上 ``/tmp`` 各是各的，
``~/.flywhl/sandbox/<类别>-<名字>`` 是它们自己的可写目录（``FLYWHL_STATE_DIR``），``npx``、``uvx`` 的缓存也放在这里——
家目录在沙箱里只读，缓存放不下就连包都装不上。工作区的读写权限和 bash 一样。
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Iterable

from ..config import flywhl_home
from .egress import EgressProxy, local_ports, parse_rules
from .sandbox import (EGRESS_PORT, SANDBOX_LEVELS, SANDBOX_NETWORK, absent_git_redirects, bwrap_argv, default_writable, detect,
                      pin_sockets, require_bwrap, sandbox_argv, sweep_git_redirects)

OFF_HINT = ('确实要在没有沙箱的环境里运行，在用户配置 ~/.flywhl/config.toml 的 [permissions] 里写 sandbox = "off"，'
            "或者启动时加 --sandbox off")
_TMP_PREFIXES = ("flywhl-sandbox-tmp-", "flywhl-egress-")


class SandboxUnavailable(RuntimeError):
    """配置了沙箱却用不了：调用方拒绝执行，不能悄悄在沙箱外运行。"""


def _remove_tmp(path: str) -> None:
    """删除前核对目标：只删 mkdtemp 在系统临时目录下建出来的目录。"""
    target = os.path.realpath(path)
    if (os.path.dirname(target) == os.path.realpath(tempfile.gettempdir())
            and os.path.basename(target).startswith(_TMP_PREFIXES)):
        shutil.rmtree(target, ignore_errors=True)


@dataclass(frozen=True)
class Launch:
    """``enter()`` 的结果：照这个启动子进程。"""

    argv: list[str]
    env: dict[str, str]
    proxy: EgressProxy | None = None


class Confinement:
    """一份解析好的沙箱配置。默认构造就是“不进沙箱”，要在沙箱外执行的调用用它。"""

    def __init__(self, level: str = "off", network: str = "allow", rules: list[Any] | None = None,
                 sockets: list[tuple[str, str]] | None = None, *, requested: str | None = None, problem: str = "",
                 state_dir: Path | None = None, protected: list[str] | None = None):
        self.level = level                  # 实际生效的档位：macos | linux | off
        self.network = network              # 实际生效的网络策略：allow | deny | allowlist
        self.rules = rules or []
        self.sockets = sockets or []        # pin_sockets() 定下的 (配置的路径, 真实位置)
        self.requested = requested or level
        self.problem = problem              # 非空：配置要沙箱，这里却用不了
        self.state_dir = state_dir
        self.protected = protected or []    # 只读的位置，比如 harness 的数据目录，见 sandbox._read_only()
        self._private_tmp: str | None = None

    @classmethod
    def resolve(cls, sandbox: str = "auto", network: str = "auto", allow: list[str] | None = None,
                unix_sockets: list[str] | None = None, protected: list[str] | None = None) -> "Confinement":
        """校验并解析配置。写错了在这里报错，不要等到第一条命令。"""
        if sandbox not in SANDBOX_LEVELS:
            raise ValueError(f"未知的沙箱档位 {sandbox!r}（可选：{' | '.join(SANDBOX_LEVELS)}）")
        if network not in SANDBOX_NETWORK:
            raise ValueError(f"未知的沙箱网络策略 {network!r}（可选：{' | '.join(SANDBOX_NETWORK)}）")
        if not isinstance(allow or [], list):
            raise ValueError(f"network_allow 必须是字符串列表，实际是 {allow!r}")
        sockets = unix_sockets or []
        if not isinstance(sockets, list) or not all(isinstance(p, str) and os.path.isabs(p) for p in sockets):
            raise ValueError(f"unix_sockets 必须是绝对路径的列表，实际是 {unix_sockets!r}")
        level, problem = sandbox, ""
        if sandbox == "auto":
            found = detect()
            level, problem = found.level or "off", "" if found.level else found.reason
        if network == "auto":
            network = "allow" if level == "off" and not problem else "allowlist"
        return cls(level, network, parse_rules(allow or []), pin_sockets(sockets),   # 套接字启动时定下真实位置，之后不再解析
                   requested=sandbox, problem=problem, protected=protected)

    @classmethod
    def from_config(cls, permissions: Any, protected: list[str] | None = None) -> "Confinement":
        return cls.resolve(permissions.sandbox, permissions.sandbox_network, permissions.network_allow,
                           permissions.unix_sockets, protected)

    @property
    def active(self) -> bool:
        """配置要求沙箱，不管它用不用得了。"""
        return self.level != "off" or bool(self.problem)

    def scoped(self, kind: str, name: str, overrides: dict[str, Any] | None = None) -> "Confinement":
        """给一个 hook / MCP 服务器取一份：独立的 /tmp 与可写目录。``overrides`` 里的 ``sandbox = "off"`` 让它在沙箱外运行，
        ``sandbox_network``、``network_allow`` 单独设网络策略，没写的跟随全局。"""
        o = overrides or {}
        if o.get("sandbox") == "off":
            return Confinement(requested="off")
        network = o.get("sandbox_network") or self.network
        rules = self.rules if o.get("network_allow") is None else parse_rules(o["network_allow"])
        safe = re.sub(r"[^\w.-]", "_", name)
        return Confinement(self.level, network, rules, self.sockets, requested=self.requested, problem=self.problem,
                           state_dir=flywhl_home() / "sandbox" / f"{kind}-{safe}" if self.level != "off" else None,
                           protected=self.protected)

    def status(self) -> dict[str, Any]:
        """给 doctor、启动横幅、「环境与安全」页看的：实际生效的状态，不是配置里写的。"""
        return {"level": self.level, "requested": self.requested, "network": self.network,
                "allow": [str(r) for r in self.rules], "unix_sockets": [p for p, _ in self.sockets],
                "problem": self.problem}

    def describe(self) -> str:
        """一行话的状态，用于终端输出。"""
        if self.problem:
            return f"系统沙箱不可用，bash 等子进程会拒绝执行：{self.problem}"
        if self.level == "off":
            return "系统沙箱已关闭"
        hosts = f"（{len(self.rules)} 条规则）" if self.network == "allowlist" else ""
        return f"系统沙箱 {self.level} · 网络 {self.network}{hosts}"

    def private_tmp(self) -> str:
        """Linux 档私有的 /tmp：命令之间照样能用临时文件传东西，又看不到宿主 /tmp 里的套接字。进程退出时删除。"""
        if self._private_tmp is None:
            self._private_tmp = tempfile.mkdtemp(prefix=_TMP_PREFIXES[0])
            atexit.register(_remove_tmp, self._private_tmp)
        return self._private_tmp

    def _scope_env(self) -> dict[str, str]:
        if self.state_dir is None:
            return {}
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        cache = self.state_dir / "cache"
        return {"FLYWHL_STATE_DIR": str(self.state_dir), "XDG_CACHE_HOME": str(cache),
                "npm_config_cache": str(cache / "npm"), "UV_CACHE_DIR": str(cache / "uv")}

    def _wrap(self, argv: list[str], workspace: str | Path, proxy: EgressProxy | None, chdir: str | None) -> list[str]:
        extra: Iterable[str] = [str(self.state_dir)] if self.state_dir else []
        if self.level == "macos":
            return sandbox_argv(argv, workspace, writable=[*default_writable(), *extra] if extra else None,
                                network=self.network, local_ports=[str(proxy.port), *local_ports(self.rules)] if proxy else (),
                                unix_sockets=[real for _, real in self.sockets], protected=self.protected)
        return bwrap_argv(argv, workspace, writable=extra, network=self.network, private_tmp=self.private_tmp(),
                          unix_sockets=self.sockets, egress_socket=proxy.unix_path if proxy else None,
                          local_ports=local_ports(self.rules), chdir=chdir, protected=self.protected)

    @contextlib.asynccontextmanager
    async def enter(self, argv: list[str], workspace: str | Path, env: dict[str, str], *,
                    chdir: str | None = None) -> AsyncIterator[Launch]:
        """准备好一次启动，退出时关闭出站代理：子进程要在 ``async with`` 里跑完。用不了沙箱时抛 ``SandboxUnavailable``。"""
        if self.problem:
            raise SandboxUnavailable(f"{self.problem}。{OFF_HINT}")
        if self.level == "off":
            if self.network != "allow":   # fail-closed：网络限制要靠沙箱执行
                raise SandboxUnavailable(f"sandbox_network = {self.network!r} 要在系统沙箱里才能生效，"
                                         "当前 sandbox = \"off\"。请同时设置 permissions.sandbox")
            yield Launch(list(argv), env)
            return
        async with contextlib.AsyncExitStack() as stack:   # 出站代理与子进程同生共死
            try:
                env = {**env, **self._scope_env()}   # 宿主的缓存目录在沙箱里写不了，以这份为准
                if self.level == "linux":
                    require_bwrap()
                    self.private_tmp()
                    stack.callback(sweep_git_redirects, absent_git_redirects(workspace))
                proxy = await self._start_proxy(stack) if self.network == "allowlist" else None
                wrapped = await asyncio.to_thread(self._wrap, argv, workspace, proxy, chdir)   # 大工作区要遍历一阵
            except (RuntimeError, OSError) as e:   # OSError：TMPDIR 太长，放不下 Unix 套接字的路径；状态目录建不出来
                raise SandboxUnavailable(f"{e}。{OFF_HINT}") from None
            yield Launch(wrapped, {**env, **proxy.env(EGRESS_PORT if self.level == "linux" else None)} if proxy else env, proxy)

    async def _start_proxy(self, stack: contextlib.AsyncExitStack) -> EgressProxy:
        if self.level == "macos":
            return await stack.enter_async_context(EgressProxy(self.rules))
        private = tempfile.mkdtemp(prefix=_TMP_PREFIXES[1])   # 0700：只有 harness 自己碰得到代理的套接字
        stack.callback(_remove_tmp, private)
        return await stack.enter_async_context(EgressProxy(self.rules, unix_path=os.path.join(private, "proxy.sock")))


__all__ = ["Confinement", "Launch", "OFF_HINT", "SandboxUnavailable"]
