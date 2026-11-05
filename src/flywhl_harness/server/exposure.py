"""``flywhl serve`` 启动前的网络暴露判定。

控制台能批准命令、切换权限，所以“谁连得上”要在监听之前判清楚。这里都是纯函数：输入命令行参数和解析好的沙箱配置，
不碰网络和文件；打印和退出码归 ``cmd_serve``。

``--no-token`` 的判定
---------------------
没有令牌时，``POST /api/approvals/{aid}`` 只剩 Host 与 ``Origin`` 两道检查，而命令行客户端发给 ``127.0.0.1`` 的请求
两道都过得去。沙箱里的命令连得到控制台端口，就能批准自己触发的人工门。``console_reachers()`` 找出这样的通道。
"""

from __future__ import annotations

import ipaddress
from typing import Any, Iterable, Mapping

from ..config import HookConfig
from ..tools.confine import Confinement
from ..tools.egress import AllowRule
from .app import LOOPBACK_HOSTS

MIN_TOKEN_LENGTH = 16

# 命令连本机时能写的目标：(写给代理的主机名, 连接用的地址)。localhost 与 127.0.0.1 在代理那里是同一台机器。
# 0.0.0.0 要单独列：连它落到本机，而 AllowRule.reaches() 放行写全 IP 的规则，network_allow 里写 0.0.0.0:端口 同样连得到
_LOOPBACK_ROUTES = (("127.0.0.1", "127.0.0.1"), ("localhost", "127.0.0.1"), ("localhost", "::1"), ("0.0.0.0", "0.0.0.0"))


def is_wildcard(host: str) -> bool:
    """监听所有网卡。IPv6 的零地址有多种写法，asyncio 里空字符串也表示所有网卡。"""
    if not host:
        return True
    try:
        return ipaddress.ip_address(host).is_unspecified
    except ValueError:   # 主机名
        return False


def serve_refusal(host: str, *, expose: bool, no_token: bool, token: str | None, allow_hosts: Iterable[str]) -> str | None:
    """只看命令行参数就能判定的拒绝理由，``None`` 表示可以继续。``token`` 是 ``--token`` 的原值，没写时是 ``None``。"""
    if "*" in allow_hosts:
        return "--allow-host 不接受 *：Host 白名单挡的是 DNS 重绑定，不能关闭。请写出访问用的主机名或 IP，可重复"
    if host in LOOPBACK_HOSTS:
        return None
    if no_token:
        return f"--no-token 只能用于回环地址（127.0.0.1 / localhost / ::1），当前监听 {host}"
    if not expose:
        return (f"监听 {host} 时，网络上的其他机器也连得到这个控制台，而它能批准命令、切换权限。\n"
                "确认要这样做就加 --expose；只在本机用就去掉 --host")
    if token is not None and len(token) < MIN_TOKEN_LENGTH:
        return (f"--token 用于非回环地址时至少要 {MIN_TOKEN_LENGTH} 个字符，当前 {len(token)} 个。"
                "不写 --token 就用 ~/.flywhl/server_token 里生成的令牌")
    return None


def console_hosts(host: str, allow_hosts: Iterable[str]) -> list[str]:
    """回环地址之外还要信任的 Host：用户写的，加上具体的监听地址。通配地址不是任何人访问时用的主机名，不进白名单。"""
    return [*allow_hosts, *([] if host in LOOPBACK_HOSTS or is_wildcard(host) else [host])]


def console_url(host: str, port: int, token: str | None) -> str:
    """启动时打印的链接。通配地址不在白名单里，换成回环地址，这个链接在本机总打得开。"""
    if is_wildcard(host):
        host = "::1" if ":" in host else "127.0.0.1"
    shown = f"[{host}]" if ":" in host else host
    return f"http://{shown}:{port}/" + (f"?token={token}" if token else "")


def rules_reach_loopback(rules: Iterable[AllowRule], port: int) -> bool:
    """出站白名单放不放行到本机 ``port`` 的连接。判定和代理相同：规则匹配主机与端口，并且接受解析出的地址。"""
    return any(r.matches(name, port) and r.reaches(ipaddress.ip_address(ip)) for r in rules for name, ip in _LOOPBACK_ROUTES)


def _reach_reason(box: Confinement, port: int) -> str:
    """这份沙箱配置下的命令为什么连得到本机 ``port``；连不到时是空串。"""
    if box.problem:        # 配置了沙箱却用不了：命令根本不会执行
        return ""
    if box.level == "off":
        return "不在系统沙箱里"
    if box.network == "allow":
        return "沙箱网络策略是 allow"
    if box.network == "allowlist" and rules_reach_loopback(box.rules, port):
        return "出站白名单放行了这个端口"
    return ""


def console_reachers(box: Confinement, port: int, hooks: Iterable[HookConfig] = (),
                     mcp_servers: Mapping[str, dict[str, Any]] | None = None) -> list[str]:
    """沙箱里连得到控制台端口的通道，每条写明原因；连不到时是空列表。

    hook 与 MCP 服务器按 ``scoped()`` 算实际生效的配置，不能只看全局：全局沙箱用不了时，写了 ``sandbox = "off"`` 的那一条
    照样在沙箱外运行；``network_allow`` 是替换全局规则，不是追加。"""
    entries = [("bash 与验收命令", box)]
    entries += [(f"hook {i}（{hc.event}）", box.scoped("hook", f"{i}-{hc.event}", vars(hc))) for i, hc in enumerate(hooks)]
    entries += [(f"MCP 服务器 {name}", box.scoped("mcp", name, spec)) for name, spec in (mcp_servers or {}).items()]
    return [f"{label}：{why}" for label, scoped in entries if (why := _reach_reason(scoped, port))]


def no_token_refusal(port: int, reachers: Iterable[str]) -> str:
    return (f"--no-token 被拒绝：Agent 运行的命令连得到控制台端口 {port}（{'；'.join(reachers)}）。\n"
            "没有令牌时，这些命令能直接调用审批接口，批准自己的人工门。\n"
            "修法：去掉 --no-token，或者收紧上面列出的沙箱设置，让命令连不到这个端口")


__all__ = ["MIN_TOKEN_LENGTH", "console_hosts", "console_reachers", "console_url", "is_wildcard", "no_token_refusal",
           "rules_reach_loopback", "serve_refusal"]
