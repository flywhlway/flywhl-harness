"""命令行入口：``flywhl``。

    flywhl serve                 启动 Web UI（默认 http://127.0.0.1:8765）
    flywhl chat                  终端交互（Ctrl-C 取消当前运行，连按两次退出）
    flywhl run "列出目录"         一次性执行；--json 输出 JSONL 事件流（便于脚本/CI 集成）
    flywhl sessions | runs       查看会话与运行
    flywhl resume <run_id>       恢复暂停/中断的运行
    flywhl retry <run_id>        重试失败的运行（从持久化的历史继续，attempt + 1）
    flywhl recover               接管心跳过期（Worker 崩溃）的运行并自动恢复
    flywhl replay <run_id>       确定性回放；--fork-at N 从第 N 次模型调用起换成当前模型（反事实回放）
    flywhl trace <run_id>        导出轨迹（含 OTel GenAI 风格的 span）
    flywhl tools                 列出工具及其副作用等级
    flywhl doctor                模型接入自检：对每个配置了密钥的供应商跑流式文本 + 工具往返

启动时读取当前目录的 ``.env``，但只接受各家密钥与模型选择类变量（见 ``IMPLICIT_ENV_KEYS``）；
地址、代理、权限模式等变量要么写进环境，要么用 ``--env-file`` 显式指定（表示你信任这个文件）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import signal
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__
from .config import HarnessConfig, load_dotenv, read_dotenv
from .events import E, Event
from .llm.profiles import PROFILES
from .policy.approvals import ApprovalDecision
from .runtime.manager import Harness

C = {"dim": "\033[2m", "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m", "blue": "\033[34m",
     "cyan": "\033[36m", "bold": "\033[1m", "reset": "\033[0m"}
if not sys.stdout.isatty():
    C = {k: "" for k in C}


def _c(color: str, text: str) -> str:
    return f"{C[color]}{text}{C['reset']}"


def _fmt_tokens(n: int) -> str:
    return f"{n / 1000:.1f}K" if n >= 1000 else str(n)


class TerminalRenderer:
    """把事件流渲染到终端。只依赖事件，不依赖 harness 内部——任何事件消费者都可以这样写。"""

    def __init__(self, show_thinking: bool = False):
        self.show_thinking = show_thinking
        self.in_text = False
        self.in_thinking = False

    def _nl(self) -> None:
        if self.in_text or self.in_thinking:
            print()
        self.in_text = self.in_thinking = False

    def __call__(self, ev: Event) -> None:
        d = ev.data
        t = ev.type
        if t == E.MESSAGE_UPDATE:
            if d["kind"] == "text":
                if self.in_thinking:
                    self._nl()
                print(d["delta"], end="", flush=True)
                self.in_text = True
            elif d["kind"] == "thinking" and self.show_thinking:
                print(_c("dim", d["delta"]), end="", flush=True)
                self.in_thinking = True
        elif t == E.TOOL_START:
            self._nl()
            print(_c("cyan", f"⚙ {d.get('summary') or d['name']}") + _c("dim", f"  [{d.get('effect')}]"))
        elif t == E.TOOL_UPDATE:
            up = d.get("update") or {}
            if up.get("output"):
                print(_c("dim", up["output"].rstrip("\n")), flush=True)
            elif (sub := up.get("subagent")) and (sub.get("event") or {}).get("type") == E.TOOL_START:
                # 子代理的事件包在父 Run 的 tool_update 里透传：CLI 只显示它调用了哪些工具
                sd = sub["event"].get("data") or {}
                print(_c("dim", f"    ↳ [{sub.get('description')}] {sd.get('summary') or sd.get('name')}"), flush=True)
        elif t == E.TOOL_END:
            first = (d.get("content") or "").strip().splitlines()[:1]
            mark = _c("red", "  ✗") if d.get("is_error") else _c("green", "  ✓")
            print(f"{mark} {_c('dim', str(d.get('status')))} {_c('dim', (first[0] if first else '')[:120])}"
                  + _c("dim", f"  {d.get('duration_ms', 0)}ms"))
        elif t == E.COMPACTION_START:
            self._nl()
            print(_c("yellow", f"⟲ 上下文压缩：{_fmt_tokens(d['tokens'])} / {_fmt_tokens(d['window'])} tokens"))
        elif t == E.COMPACTION_END and not d.get("skipped"):
            print(_c("yellow", f"  压缩完成：{_fmt_tokens(d['tokens_before'])} → {_fmt_tokens(d['tokens_after'])}"
                                f"（摘要了 {d['summarized']} 条消息）"))
        elif t == E.NOTICE:
            self._nl()
            print(_c("yellow", f"! {d.get('text')}"))
        elif t == E.MODEL_RETRY:
            self._nl()
            print(_c("yellow", f"↻ 模型调用重试 #{d.get('attempt')}（{d.get('kind')}），{d.get('delay')}s 后重试"))
        elif t == E.RUN_END:
            self._nl()
            u = d.get("usage") or {}
            color = "green" if d["status"] == "completed" else "yellow"
            tin = u.get("input_tokens", 0) + u.get("cache_read_tokens", 0) + u.get("cache_write_tokens", 0)
            cache = f"（缓存命中 {_fmt_tokens(u['cache_read_tokens'])}）" if u.get("cache_read_tokens") else ""
            print(_c(color, f"■ {d['status']}") + _c("dim", f"  {d.get('reason', '')} · {d.get('turns', 0)} 轮 · "
                  f"输入 {_fmt_tokens(tin)}{cache} / 输出 {_fmt_tokens(u.get('output_tokens', 0))} tokens"
                  f" · ${u.get('cost_usd', 0):.4f}"))


async def terminal_approval(rec) -> ApprovalDecision:
    """终端审批：展示真实动作与参数（而不是模型的自我描述）。"""
    print()
    print(_c("bold", f"⚠ 需要审批：{rec.summary}") + _c("dim", f"  [风险 {rec.risk}] {rec.reason}"))
    print(_c("dim", json.dumps(rec.args, ensure_ascii=False, indent=2)[:1500]))
    ans = (await asyncio.to_thread(input, "  [y] 允许  [n] 拒绝  [a] 本会话总是允许 > ")).strip().lower()
    if ans in ("y", "yes", "a", "是"):
        return ApprovalDecision(True, by="terminal-user", remember=ans == "a")
    reason = "" if ans in ("n", "no", "") else ans
    return ApprovalDecision(False, by="terminal-user", reason=reason)


# 启动目录的 .env 可能来自不可信仓库（工作区默认就是启动目录），只放行“最多换一家官方端点或多花点钱”的变量。
# 用白名单而不是黑名单：*_BASE_URL、HTTPS_PROXY + SSL_CERT_FILE、权限模式、数据目录……能被滥用的变量列不全。
IMPLICIT_ENV_KEYS = frozenset({k for p in PROFILES.values() for k in p.key_env} | {
    "FLYWHL_MODEL", "FLYWHL_FALLBACK_MODELS", "LLM_PRIMARY_MODEL", "LLM_FALLBACK_MODEL",
    "FLYWHL_THINKING", "FLYWHL_REASONING_EFFORT", "FLYWHL_CNY_PER_USD",
})


def load_env(env_file: str | None) -> tuple[list[str], list[str]]:
    """返回 (加载的变量名, 被忽略的变量名)。``--env-file`` 是用户显式信任的文件，全部加载。"""
    if env_file:
        return load_dotenv(env_file), []
    ignored = [k for k in read_dotenv(".env") if k not in IMPLICIT_ENV_KEYS and k not in os.environ]
    return load_dotenv(".env", allow=IMPLICIT_ENV_KEYS.__contains__), ignored


def _config(args: argparse.Namespace) -> HarnessConfig:
    cfg = HarnessConfig.load(getattr(args, "config", None), workspace=getattr(args, "workspace", None),
                             provider=getattr(args, "provider", None), model=getattr(args, "model", None))
    if getattr(args, "mode", None):
        cfg.permissions.mode = args.mode
    if getattr(args, "yes", False):
        cfg.permissions.on_ask = "allow"
    return cfg


async def _stream_run(h: Harness, session_id: str, prompt: str, as_json: bool, show_thinking: bool) -> str:
    render = TerminalRenderer(show_thinking)
    run = await h.start_run(session_id, prompt)
    loop = asyncio.get_running_loop()
    presses: list[float] = []

    def on_sigint() -> None:
        presses.append(time.time())
        if len(presses) >= 2 and presses[-1] - presses[-2] < 1.5:
            raise SystemExit(130)
        print(_c("yellow", "\n⏹ 正在取消（协作式：在途的不可中断操作会先完成）…再按一次 Ctrl-C 强制退出"))
        asyncio.ensure_future(h.cancel(run.id, reason="用户按下 Ctrl-C", by="terminal-user"))

    with _sigint(loop, on_sigint):
        async for ev in h.subscribe(run.id):
            if as_json:
                print(json.dumps(ev.to_dict(), ensure_ascii=False, default=str), flush=True)
            else:
                render(ev)
    rec = await h.wait(run.id)
    return rec.status


class _sigint:
    def __init__(self, loop: asyncio.AbstractEventLoop, cb):
        self.loop, self.cb = loop, cb

    def __enter__(self):
        try:
            self.loop.add_signal_handler(signal.SIGINT, self.cb)
        except (NotImplementedError, RuntimeError):
            pass
        return self

    def __exit__(self, *exc):
        try:
            self.loop.remove_signal_handler(signal.SIGINT)
        except (NotImplementedError, RuntimeError):
            pass


# ====================================================================== 子命令
async def cmd_run(args: argparse.Namespace) -> int:
    cfg = _config(args)
    if args.json and cfg.permissions.on_ask == "wait":
        cfg.permissions.on_ask = "deny"  # 无人值守：需要审批的操作一律拒绝（可用 --yes 改为放行）
    async with Harness(cfg, approval_resolver=None if args.json else terminal_approval) as h:
        s = h.create_session(args.prompt[:30])
        status = await _stream_run(h, s.id, args.prompt, args.json, args.thinking)
    return 0 if status == "completed" else 1


async def cmd_chat(args: argparse.Namespace) -> int:
    cfg = _config(args)
    async with Harness(cfg, approval_resolver=terminal_approval) as h:
        s = h.create_session("终端会话")
        print(_c("bold", f"flywhl-harness {__version__}") + _c("dim", f"  {h.provider.name}/{h.model} · 模式 "
              f"{cfg.permissions.mode} · 工作区 {h.workspace_root}"))
        print(_c("dim", "输入任务开始；/help 查看命令；Ctrl-D 退出"))
        while True:
            try:
                line = (await asyncio.to_thread(input, _c("blue", "\n› "))).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not line:
                continue
            if line.startswith("/"):
                cmd, _, rest = line.partition(" ")
                if cmd in ("/exit", "/quit"):
                    return 0
                if cmd == "/help":
                    print("/new 新会话  /mode <default|accept_edits|plan|bypass>  /compact 压缩上下文  "
                          "/tree 会话树  /branch <entry_id> 切换分支  /exit 退出")
                elif cmd == "/new":
                    s = h.create_session("终端会话")
                    print(_c("dim", f"新会话 {s.id}"))
                elif cmd == "/mode":
                    h.set_mode(s.id, rest.strip())
                    print(_c("dim", f"权限模式：{rest.strip()}"))
                elif cmd == "/compact":
                    res = await h.compact(s.id)
                    print(_c("dim", f"压缩：{res['tokens_before']} → {res['tokens_after']} tokens" if res else "没有可压缩的历史"))
                elif cmd == "/tree":
                    for n in h.log(s.id).tree():
                        mark = "●" if n["head"] else ("│" if n["on_path"] else " ")
                        print(_c("dim", f"{mark} {n['id']}  {n['label']}"))
                elif cmd == "/branch":
                    h.branch(s.id, rest.strip() or None)
                    print(_c("dim", "已切换分支，下一条输入将从该处继续"))
                else:
                    print(_c("red", f"未知命令 {cmd}"))
                continue
            await _stream_run(h, s.id, line, False, args.thinking)


async def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .server.app import create_app

    from .server.app import LOOPBACK_HOSTS

    cfg = _config(args)
    h = Harness(cfg)
    loopback = args.host in LOOPBACK_HOSTS
    # 监听非回环地址时，局域网里的任何人都能访问：必须带访问令牌
    token = args.token or (None if loopback else secrets.token_urlsafe(24))
    allow = list(args.allow_host or []) + ([] if loopback else ["*" if args.host in ("0.0.0.0", "::") else args.host])
    app = create_app(h, token=token, allowed_hosts=allow)
    url = f"http://{args.host}:{args.port}/" + (f"?token={token}" if token else "")
    print(_c("bold", f"flywhl-harness {__version__}") + f"  {url}  "
          + _c("dim", f"({h.provider.name}/{h.model}，工作区 {h.workspace_root})"))
    server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, log_level="warning"))
    await server.serve()
    return 0


async def cmd_sessions(args: argparse.Namespace) -> int:
    h = Harness(_config(args))
    for s in h.store.list_sessions():
        runs = h.store.list_runs(s.id, limit=1)
        st = runs[0].status if runs else "-"
        print(f"{s.id}  {st:<16} {time.strftime('%m-%d %H:%M', time.localtime(s.updated_at))}  {s.title}")
    return 0


async def cmd_runs(args: argparse.Namespace) -> int:
    h = Harness(_config(args))
    for r in h.store.list_runs(args.session, limit=50):
        u = r.usage or {}
        print(f"{r.id}  {r.status:<16} attempt={r.attempt} v={r.version}  tokens={u.get('input_tokens', 0)}+"
              f"{u.get('output_tokens', 0)}  {(r.prompt or '')[:40]}")
    return 0


async def cmd_resume(args: argparse.Namespace) -> int:
    async with Harness(_config(args), approval_resolver=terminal_approval) as h:
        await (h.retry(args.run_id) if args.cmd == "retry" else h.resume(args.run_id))
        render = TerminalRenderer()
        async for ev in h.subscribe(args.run_id, h.store.max_event_seq(args.run_id)):
            render(ev)
        rec = await h.wait(args.run_id)
    return 0 if rec.status == "completed" else 1


async def cmd_recover(args: argparse.Namespace) -> int:
    async with Harness(_config(args), approval_resolver=terminal_approval) as h:
        ids = await h.recover(auto_resume=True)
        print(f"接管并恢复了 {len(ids)} 个运行：{ids}")
        for rid in ids:
            await h.wait(rid)
            print(rid, h.store.get_run(rid).status)  # type: ignore[union-attr]
    return 0


async def cmd_replay(args: argparse.Namespace) -> int:
    from .observe.replay import replay_run

    h = Harness(_config(args))
    live = h.provider if args.fork_at is not None else None
    rep = await replay_run(h.store, args.run_id, config=h.config, live=live, live_model=h.model,
                           fork_at=args.fork_at, base_tools=h.tools)
    print(json.dumps(rep.to_dict(), ensure_ascii=False, indent=2))
    return 0


async def cmd_trace(args: argparse.Namespace) -> int:
    from .observe.trace import dumps, export_trajectory

    h = Harness(_config(args))
    text = dumps(export_trajectory(h.store, args.run_id))
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"已导出到 {args.output}")
    else:
        print(text)
    return 0


async def cmd_tools(args: argparse.Namespace) -> int:
    async with Harness(_config(args)) as h:
        for t in h.tools:
            print(f"{t.name:<28} {str(t.effect):<9} {'可中断' if t.interruptible else '不可中断':<5}  {t.description[:60]}")
    return 0


async def cmd_doctor(args: argparse.Namespace) -> int:
    from .llm.doctor import check_all, format_report

    cfg = _config(args)
    specs = args.models or None
    print(_c("dim", "逐个供应商检查：流式文本 → 工具调用往返（每项一两次很小的真实调用）"))
    results = await check_all(cfg, specs, timeout=args.timeout)
    print(format_report(results, color=bool(C["reset"])))
    return 0 if results and all(r.ok for r in results) else 1


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="flywhl", description="flywhl-harness：从零手写的最小 Agent Harness")
    p.add_argument("--version", action="version", version=__version__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-w", "--workspace", help="工作区目录（默认当前目录）")
    common.add_argument("-c", "--config", help="配置文件（默认 ./flywhl.toml）")
    common.add_argument("--provider", choices=["auto", *PROFILES])
    common.add_argument("--model", help="模型，写成 <供应商>:<模型>（如 deepseek:deepseek-flash）或只写模型名")
    common.add_argument("--env-file", help="启动前加载的 .env 文件（默认当前目录的 .env）")
    common.add_argument("--mode", choices=["default", "accept_edits", "plan", "bypass"], help="权限模式")
    common.add_argument("-y", "--yes", action="store_true", help="需要审批的操作自动放行（仅限可信环境）")
    common.add_argument("--thinking", action="store_true", help="显示模型的思考过程")
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("serve", parents=[common], help="启动 Web UI")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--token", help="访问令牌（监听非回环地址时默认自动生成）")
    sp.add_argument("--allow-host", action="append", help="额外信任的 Host 主机名（反向代理、自定义域名），可重复")
    sp.set_defaults(fn=cmd_serve)
    sub.add_parser("chat", parents=[common], help="终端交互").set_defaults(fn=cmd_chat)
    sp = sub.add_parser("run", parents=[common], help="一次性执行")
    sp.add_argument("prompt")
    sp.add_argument("--json", action="store_true", help="输出 JSONL 事件流")
    sp.set_defaults(fn=cmd_run)
    sub.add_parser("sessions", parents=[common], help="列出会话").set_defaults(fn=cmd_sessions)
    sp = sub.add_parser("runs", parents=[common], help="列出运行")
    sp.add_argument("--session")
    sp.set_defaults(fn=cmd_runs)
    sp = sub.add_parser("resume", parents=[common], help="恢复运行")
    sp.add_argument("run_id")
    sp.set_defaults(fn=cmd_resume, cmd="resume")
    sp = sub.add_parser("retry", parents=[common], help="重试失败的运行")
    sp.add_argument("run_id")
    sp.set_defaults(fn=cmd_resume, cmd="retry")
    sub.add_parser("recover", parents=[common], help="接管崩溃的运行").set_defaults(fn=cmd_recover)
    sp = sub.add_parser("replay", parents=[common], help="回放运行")
    sp.add_argument("run_id")
    sp.add_argument("--fork-at", type=int, help="从第 N 次模型调用起改用当前配置的模型")
    sp.set_defaults(fn=cmd_replay)
    sp = sub.add_parser("trace", parents=[common], help="导出轨迹")
    sp.add_argument("run_id")
    sp.add_argument("-o", "--output")
    sp.set_defaults(fn=cmd_trace)
    sub.add_parser("tools", parents=[common], help="列出工具").set_defaults(fn=cmd_tools)
    sp = sub.add_parser("doctor", parents=[common], help="模型接入自检")
    sp.add_argument("models", nargs="*", help="要检查的模型（默认：每个配置了密钥的供应商的默认模型）")
    sp.add_argument("--timeout", type=float, default=90.0, help="单个检查的超时秒数")
    sp.set_defaults(fn=cmd_doctor)

    args = p.parse_args(argv)
    if not getattr(args, "fn", None):
        p.print_help()
        return
    if args.env_file and not Path(args.env_file).is_file():
        p.error(f"找不到 --env-file 指定的文件：{args.env_file}")
    _, ignored = load_env(args.env_file)
    if ignored:
        print(_c("yellow", f"! 已忽略当前目录 .env 中的 {', '.join(ignored)}：启动目录的 .env 只接受密钥与模型选择；"
                           "地址、代理、权限类变量请写进环境变量，或用 --env-file 显式指定"), file=sys.stderr)
    try:
        code = asyncio.run(args.fn(args))
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)


if __name__ == "__main__":
    main()
