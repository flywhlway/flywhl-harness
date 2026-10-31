"""HTTP 接口：REST 控制面 + SSE 事件流 + 静态 Web UI。

SSE 设计要点
------------
- **Run 与连接解耦**：Run 在后台执行；连接只是一个订阅者。用户刷新页面、网络抖动都不影响任务；
- **断线续传**：每个事件带 ``id: <seq>``；浏览器 EventSource 重连时自动带上 ``Last-Event-ID``，
  服务端从事件表补发 seq 之后的事件，再接上实时流（不重不漏）；
- **心跳**：每 15 秒发一行注释，防止代理/负载均衡因空闲断开连接；
- **断开检测**：客户端断开后停止推送（但 **不停止** Run——停止 Run 要显式调用 cancel）。

访问控制
--------
这个服务能切换权限模式、批准审批、发起运行——等于能在本机执行命令，所以“只监听 127.0.0.1”并不够：

- **Host 白名单**：挡住 DNS 重绑定。攻击者的域名解析到 127.0.0.1 后，恶意页面与本服务“同源”，
  但浏览器发来的 ``Host`` 仍是攻击者的域名；
- **跨站写请求**：改变状态的请求（非 GET / HEAD / OPTIONS）若带 ``Origin``，必须与本次请求的 ``Host`` 同源——
  主机名 **和端口** 都相同，挡住 CSRF。只比主机名不够：同一台机器上任何 ``http://127.0.0.1:<别的端口>`` 的网页
  （本地开发服务器、别的工具的控制台）都能对本服务发写请求；SameSite cookie 挡的是跨站，同主机不同端口不算跨站；
- **访问令牌**（``flywhl serve`` 默认开启，回环地址也一样；令牌持久化在 ``~/.flywhl/server_token``）：
  首次用 ``/?token=…`` 打开页面后写入 HttpOnly cookie（30 天），之后的 API 与 SSE 请求都带着它；
  也接受 ``Authorization: Bearer …``。查询串里的令牌只在 ``/`` 兑换这一次——其他路径也认的话，
  令牌会跟着链接进访问日志、浏览器历史和 Referer。只监听回环地址时不够：同一台机器上的其他用户、
  其他程序都能连上 127.0.0.1；
- **安全响应头**：CSP 禁止内联脚本与被别的页面嵌入（``frame-ancestors 'none'``，防点击劫持诱导点“批准”），
  ``nosniff``、``Referrer-Policy: no-referrer``；改变状态的请求体限制在 8 MB 以内。
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import __version__
from .._utils import estimate_tokens
from ..agent.compaction import message_tokens
from ..agent.context import ContextManager
from ..observe.export import session_markdown
from ..observe.trace import export_trajectory
from ..policy.bash_analyzer import analyze
from ..policy.permissions import MANAGED_BASELINE
from ..policy.supply_chain import mcp_argv, mcp_base, referenced_files, unpinned_packages
from ..runtime.manager import Harness
from ..runtime.state import TERMINAL, RunStatus

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class RevalidatingStaticFiles(StaticFiles):
    """静态资源每次都向服务端确认（ETag 未变时只回 304）：升级后浏览器不会卡在旧版 UI。"""

    def file_response(self, *args: Any, **kwargs: Any):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


class NewSession(BaseModel):
    title: str | None = None
    mode: str | None = None


class Prompt(BaseModel):
    prompt: str


class Steer(BaseModel):
    text: str
    mode: str = "steer"   # steer | follow_up


class ModeBody(BaseModel):
    mode: str


class BranchBody(BaseModel):
    entry_id: str | None = None


class ApprovalBody(BaseModel):
    approved: bool
    reason: str = ""
    edited_args: dict[str, Any] | None = None
    remember: bool = False


class RollbackBody(BaseModel):
    sha: str


class SessionPatch(BaseModel):
    title: str | None = None
    archived: bool | None = None


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
TOKEN_COOKIE = "flywhl_token"
TOKEN_MAX_AGE = 30 * 24 * 3600
MAX_BODY = 8 * 1024 * 1024
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_SHA = re.compile(r"[0-9a-f]{7,64}")
# 控制台只加载自己的脚本与样式：XSS 拿不到内联脚本的执行权，页面也不能被别的站点嵌进 iframe 诱导点击“批准”
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                               "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
                               "form-action 'self'; frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}
# 没带令牌就打开了控制台：给人看的说明页（API 请求照旧拿到 JSON）
_TOKEN_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>需要访问令牌 · flywhl</title></head>
<body style="font:15px/1.7 system-ui,sans-serif;max-width:34rem;margin:12vh auto;padding:0 1rem;color:#1f2328">
<h1 style="font-size:1.25rem">需要访问令牌</h1>
<p>这个控制台能批准命令、切换权限，所以默认要求访问令牌。请使用启动时终端里打印的完整链接打开（带 <code>?token=</code>），
之后浏览器会记住 30 天。</p>
<p>链接找不到了：令牌保存在 <code>~/.flywhl/server_token</code>，在地址后面加上 <code>/?token=令牌</code> 即可。</p>
</body></html>"""


def _hostname(netloc: str) -> str:
    """从 ``host:port`` / ``[::1]:port`` 中取出主机名（小写）。"""
    return (urlsplit(f"//{netloc}").hostname or "").lower()


def _same_origin(origin: str, host: str) -> bool:
    """``Origin`` 是否就是本次请求的 ``Host``：主机名与端口都相同（``Origin: null``、非 http(s) 一律不算）。"""
    o, h = urlsplit(origin), urlsplit(f"//{host}")
    if o.scheme not in ("http", "https") or not o.hostname:
        return False
    try:
        return o.hostname.lower() == (h.hostname or "").lower() and o.port == h.port
    except ValueError:  # 端口不是合法数字
        return False


def create_app(harness: Harness, *, auto_recover: bool = True, token: str | None = None,
               allowed_hosts: Iterable[str] = ()) -> FastAPI:
    """``allowed_hosts`` 追加到回环地址之外的可信主机名；含 ``"*"`` 时不校验 Host（此时必须配置 token）。"""
    hosts = set(LOOPBACK_HOSTS) | {x.lower() for x in allowed_hosts}
    any_host = "*" in hosts
    if any_host and not token:
        raise ValueError("允许任意 Host 时必须配置访问令牌")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await harness.start()
        loop_task = None
        if auto_recover:
            recovered = await harness.recover(auto_resume=True)
            if recovered:
                print(f"[flywhl] 已恢复 {len(recovered)} 个中断的运行：{recovered}")
            loop_task = asyncio.create_task(harness.recovery_loop())  # 之后周期性接管心跳过期的 Run
        yield
        if loop_task:
            loop_task.cancel()
        await harness.aclose()  # 活动 Run 在安全点暂停，重启后自动继续

    app = FastAPI(title="flywhl-harness", version=__version__, lifespan=lifespan)
    h = harness

    @app.middleware("http")
    async def guard(request: Request, call_next):
        resp = await _guard(request, call_next)
        for k, v in SECURITY_HEADERS.items():   # 错误响应也带上：401 页、403 都可能被嵌进别的页面
            resp.headers.setdefault(k, v)
        return resp

    async def _guard(request: Request, call_next):
        # 1) Host 白名单：DNS 重绑定时 Host 是攻击者的域名
        if not any_host and _hostname(request.headers.get("host", "")) not in hosts:
            return JSONResponse({"detail": "Host 不在白名单中（可用 --allow-host 添加）"}, status_code=403)
        # 2) 跨站写请求：Origin（含 "null"）必须与 Host 同源，端口也要一致（Host 已经过上面的白名单）
        origin = request.headers.get("origin")
        if request.method not in _SAFE_METHODS and origin is not None:
            if not _same_origin(origin, request.headers.get("host", "")):
                return JSONResponse({"detail": "拒绝跨站请求"}, status_code=403)
        # 3) 访问令牌：查询串里的只在 / 兑换（随后 303 去掉），其他路径只认 cookie 与 Authorization
        if token:
            auth = request.headers.get("authorization", "")
            query = request.query_params.get("token") if request.url.path == "/" else None
            supplied = (query or request.cookies.get(TOKEN_COOKIE)
                        or (auth[7:] if auth.lower().startswith("bearer ") else ""))
            if not hmac.compare_digest(supplied.encode(), token.encode()):
                if request.url.path == "/":
                    return HTMLResponse(_TOKEN_PAGE, status_code=401)
                return JSONResponse({"detail": "需要访问令牌：请用启动时打印的链接打开"}, status_code=401)
            if query:
                resp = RedirectResponse("/", status_code=303)   # 写入 cookie 后去掉地址栏里的令牌
                resp.set_cookie(TOKEN_COOKIE, token, max_age=TOKEN_MAX_AGE, httponly=True, samesite="strict")
                return resp
        # 4) 请求体上限：会话库与内存都扛不住任意大的请求。分块上传没有 Content-Length，读完之前无从判断大小，
        #    控制台与 CLI 也从不分块：直接要求带长度
        if "transfer-encoding" in request.headers:
            return JSONResponse({"detail": "请求必须带 Content-Length（不接受分块上传）"}, status_code=411)
        try:
            size = int(request.headers.get("content-length") or 0)
        except ValueError:
            size = 0
        if size > MAX_BODY:
            return JSONResponse({"detail": f"请求体超过 {MAX_BODY // 1024 // 1024} MB"}, status_code=413)
        return await call_next(request)

    def run_dict(rid: str) -> dict[str, Any]:
        r = h.store.get_run(rid)
        if r is None:
            raise HTTPException(404, f"Run 不存在：{rid}")
        d = r.to_dict()
        d["active"] = rid in h._active
        d["terminal"] = RunStatus(r.status) in TERMINAL
        h.flush_if_active(rid)
        d["last_seq"] = h.store.max_event_seq(rid)
        return d

    def session_or_404(sid: str):
        s = h.store.get_session(sid)
        if s is None:
            raise HTTPException(404, f"会话不存在：{sid}")
        return s

    def approval_dict(a) -> dict[str, Any]:
        """审批对象 + 审批人做决定需要的上下文：所属会话、变更预览（diff）、命令分析。

        预览按 **当前** 工作区内容现算：页面刷新、在审批收件箱里处理时，看到的与实时事件里的一致。
        """
        d = a.to_dict()
        s = h.store.get_session(a.session_id)
        d["session_title"] = s.title if s else None
        if s is not None and a.tool in h.tools:
            ws = h.workspace(s)
            with contextlib.suppress(Exception):
                d["preview"] = h.tools.get(a.tool).preview(a.args, SimpleNamespace(workspace=ws))
            if a.tool == "bash":
                d["analysis"] = analyze(str(a.args.get("command", "")), str(ws.root)).reasons
        return d

    # ------------------------------------------------------------------ 信息
    @app.get("/api/info")
    async def info() -> dict[str, Any]:
        mi = h.model_info
        return {
            "version": __version__, "provider": h.provider.name, "model": h.model,
            "model_info": {"context_window": h.config.context.context_window or mi.context_window,
                           "max_output": mi.max_output, "input_price": mi.input_price, "output_price": mi.output_price,
                           "currency": mi.currency},
            "workspace": str(h.workspace_root), "mode": h.config.permissions.mode,
            "tools": [{"name": t.name, "description": t.description, "effect": str(t.effect)} for t in h.tools],
            "budget": h.config.budget.__dict__, "worker_id": h.worker_id,
        }

    @app.get("/api/settings")
    async def settings() -> dict[str, Any]:
        """「环境与安全」页：当前生效的配置（只读）。改配置走 flywhl.toml 与信任确认，页面不提供写入口——
        否则拿到控制台的人就能绕过信任机制给 Agent 放权。MCP 只给名字、工具数与注解策略：启动参数里常带密钥。"""
        c, pc = h.config, h.config.permissions
        names = [t.name for t in h.tools]

        def mcp_entry(n: str, spec: dict[str, Any]) -> dict[str, Any]:
            prefix = f"mcp__{n}__"
            tools = [t for t in h.tools if t.name.startswith(prefix)]
            argv = mcp_argv(spec)
            return {"name": n, "transport": "stdio" if spec.get("command") else "http", "tools": len(tools),
                    "trust_annotations": spec.get("trust_annotations") is True,
                    "read_only": [t.name.removeprefix(prefix) for t in tools if str(t.effect) == "read"],
                    "ignored_hints": sum(1 for t in tools if getattr(t, "ignored_hints", None)),
                    # 供应链：引用的文件只给路径，没锁版本的只给包名（URL 里的账号、查询串去掉）——完整的启动参数里常带密钥
                    "files": [str(f) for f in referenced_files(argv, mcp_base(spec, h.workspace_root), spec.get("env"))],
                    "unpinned": unpinned_packages(argv, mcp_base(spec, h.workspace_root), spec.get("env"))}

        return {
            "workspace": str(h.workspace_root),
            "model": {"provider": h.provider.name, "model": h.model, "fallback": list(c.fallback_models),
                      "context_window": c.context.context_window or h.model_info.context_window,
                      "max_output": h.model_info.max_output},
            "permissions": {"mode": pc.mode, "on_ask": pc.on_ask, "approval_timeout": pc.approval_timeout,
                            "rules": list(pc.rules), "baseline": len(MANAGED_BASELINE),
                            "strip_secret_env": pc.strip_secret_env, "require_pinned": pc.require_pinned},
            "sandbox": {"level": pc.sandbox, "network": pc.sandbox_network, "allow": list(pc.network_allow),
                        "unix_sockets": list(pc.unix_sockets)},
            "access": {"token": bool(token), "hosts": sorted(hosts), "any_host": any_host},
            "hooks": [{"event": hc.event, "matcher": hc.matcher, "command": hc.command, "timeout": hc.timeout,
                       "fail_closed": hc.closed, "env": sorted({*hc.env, *hc.pass_env}),   # 只给变量名
                       "files": [str(f) for f in referenced_files(hc.command, h.workspace_root, hc.env)],
                       "unpinned": unpinned_packages(hc.command, h.workspace_root, hc.env)} for hc in c.hooks],
            "mcp": [mcp_entry(n, spec) for n, spec in c.mcp_servers.items()],
            "budget": dict(c.budget.__dict__), "snapshots": c.snapshots, "verify_command": c.verify_command,
            "tools": len(names),
        }

    # ------------------------------------------------------------------ 会话
    @app.get("/api/sessions")
    async def sessions(archived: bool = False) -> list[dict[str, Any]]:
        out = []
        for s in h.store.list_sessions(limit=200):
            if bool(s.meta.get("archived")) != archived:
                continue
            runs = h.store.list_runs(s.id, limit=1)
            out.append({"id": s.id, "title": s.title, "updated_at": s.updated_at, "mode": s.meta.get("mode"),
                        "archived": archived, "last_run": runs[0].to_dict() if runs else None,
                        "active_run": h.active_run_of(s.id)})
        return out

    @app.post("/api/sessions")
    async def new_session(body: NewSession) -> dict[str, Any]:
        s = h.create_session(body.title or "新会话", mode=body.mode)
        return {**s.to_dict(), "meta": {k: v for k, v in s.meta.items() if k != "system_prompt"}}

    @app.get("/api/sessions/{sid}")
    async def session_detail(sid: str) -> dict[str, Any]:
        s = session_or_404(sid)
        log = h.log(sid)
        view = log.context()
        return {
            "session": {**s.to_dict(), "meta": {k: v for k, v in s.meta.items() if k != "system_prompt"}},
            "messages": [m.to_dict() for m in log.messages()],
            # 时间线：当前分支上的条目（消息 + 压缩摘要），UI 按它渲染，压缩点显示为折叠分隔线
            "timeline": [{"id": e.id, "kind": e.kind, "run_id": e.run_id,
                          **({"message": e.payload} if e.kind == "message" else {"payload": e.payload})}
                         for e in log.path()],
            "context_ids": view.entry_ids,
            "compaction": view.compaction.payload if view.compaction else None,
            "runs": [r.to_dict() for r in h.store.list_runs(sid, limit=50)],
            "active_run": run_dict(ar) if (ar := h.active_run_of(sid)) else None,
            "approvals": [approval_dict(a) for a in h.store.list_approvals("pending")],
        }

    @app.patch("/api/sessions/{sid}")
    async def patch_session(sid: str, body: SessionPatch) -> dict[str, Any]:
        """重命名、归档。只改元数据：不刷新 updated_at，会话在列表里的位置不变。"""
        s = session_or_404(sid)
        fields: dict[str, Any] = {}
        if body.title is not None:
            title = " ".join(body.title.split())[:80]   # 折叠换行与连续空白：标题只占一行
            if not title:
                raise HTTPException(400, "标题不能为空")
            fields["title"] = title
        if body.archived is not None:
            if body.archived and h.active_run_of(sid):
                raise HTTPException(409, "会话正在运行，结束后再归档")
            fields["meta"] = {**s.meta, "archived": body.archived}
        h.store.update_session(sid, touch=False, **fields)
        s = session_or_404(sid)
        return {"id": s.id, "title": s.title, "archived": bool(s.meta.get("archived"))}

    @app.get("/api/sessions/{sid}/export")
    async def export_session(sid: str, injected: bool = False) -> Response:
        """当前分支的对话记录，Markdown 格式（给人读；可回放的轨迹见 /api/runs/{rid}/trajectory）。"""
        s = session_or_404(sid)
        name = re.sub(r"[\\/:*?\"<>|\s]+", "-", s.title).strip("-.")[:60] or s.id
        return Response(session_markdown(h.store, sid, include_injected=injected),
                        media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}.md"})

    @app.get("/api/sessions/{sid}/tree")
    async def session_tree(sid: str) -> list[dict[str, Any]]:
        session_or_404(sid)
        return h.log(sid).tree()

    @app.get("/api/sessions/{sid}/context")
    async def context_inspector(sid: str) -> dict[str, Any]:
        """上下文检查器：模型此刻会看到什么、每部分占多少 token、离压缩阈值还有多远。"""
        s = session_or_404(sid)
        view = h.log(sid).context()
        cm = ContextManager(h.config.context, h.model_info)
        sp = s.meta.get("system_prompt", "")
        specs = h.tools.specs()
        stats = cm.estimate(view.messages, sp, specs)
        items = [{"id": m.id, "role": m.role, "kind": m.kind, "tool": m.tool_name, "tokens": message_tokens(m),
                  "preview": (m.text or ", ".join(c.name for c in m.tool_calls))[:160]} for m in view.messages]
        return {"stats": stats.to_dict(), "system_tokens": estimate_tokens(sp),
                "tools_tokens": estimate_tokens(json.dumps(specs, ensure_ascii=False)),
                "messages_tokens": sum(i["tokens"] for i in items), "items": items,
                "system_prompt": sp, "compaction": view.compaction.payload if view.compaction else None}

    @app.post("/api/sessions/{sid}/mode")
    async def set_mode(sid: str, body: ModeBody) -> dict[str, Any]:
        session_or_404(sid)
        try:
            h.set_mode(sid, body.mode)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "mode": body.mode}

    @app.post("/api/sessions/{sid}/runs")
    async def start(sid: str, body: Prompt) -> dict[str, Any]:
        session_or_404(sid)
        try:
            run = await h.start_run(sid, body.prompt)
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        return run_dict(run.id)

    @app.post("/api/sessions/{sid}/branch")
    async def branch(sid: str, body: BranchBody) -> dict[str, Any]:
        session_or_404(sid)
        try:
            h.branch(sid, body.entry_id)
        except (RuntimeError, KeyError) as e:
            raise HTTPException(409, str(e))
        return {"ok": True}

    @app.post("/api/sessions/{sid}/compact")
    async def compact(sid: str) -> dict[str, Any]:
        session_or_404(sid)
        try:
            res = await h.compact(sid)
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        return {"ok": res is not None, "result": res}

    @app.get("/api/sessions/{sid}/snapshots")
    async def snapshots(sid: str) -> list[dict[str, Any]]:
        ws = h.workspace(session_or_404(sid))
        return await ws.shadow.log() if ws.shadow else []

    @app.post("/api/sessions/{sid}/rollback")
    async def rollback(sid: str, body: RollbackBody) -> dict[str, Any]:
        s = session_or_404(sid)
        if h.active_run_of(sid):
            raise HTTPException(409, "会话正在运行，无法回滚")
        ws = h.workspace(s)
        if ws.shadow is None:
            raise HTTPException(400, "未启用快照")
        if not _SHA.fullmatch(body.sha):   # 原样拼进 git 参数：--output=… 这类选项能在任意位置写文件
            raise HTTPException(400, "快照编号必须是 7–64 位十六进制 commit sha")
        try:
            changed = await ws.shadow.restore(body.sha)
        except (RuntimeError, ValueError) as e:
            raise HTTPException(400, f"回滚失败：{e}")
        return {"ok": True, "changed": changed}

    # ------------------------------------------------------------------ Run
    @app.get("/api/runs")
    async def runs(limit: int = 200, children: bool = False) -> list[dict[str, Any]]:
        """跨会话的运行列表（运行记录页）。默认不含子代理的 Run——它们在父 Run 的 task 调用里查看。"""
        titles: dict[str, str] = {}
        out = []
        for r in h.store.list_runs(limit=max(1, min(limit, 1000))):
            if r.parent_run_id and not children:
                continue
            if r.session_id not in titles:
                s = h.store.get_session(r.session_id)
                titles[r.session_id] = s.title if s else ""
            d = {k: v for k, v in r.to_dict().items() if k not in ("checkpoint", "config")}
            d["session_title"] = titles[r.session_id]
            d["active"] = r.id in h._active
            out.append(d)
        return out

    @app.get("/api/runs/{rid}")
    async def run_detail(rid: str) -> dict[str, Any]:
        d = run_dict(rid)
        d["steps"] = [st.__dict__ for st in h.store.list_steps(rid)]
        return d

    @app.post("/api/runs/{rid}/cancel")
    async def cancel(rid: str) -> dict[str, Any]:
        return {"ok": await h.cancel(rid, reason="用户在 Web UI 点击停止", by="web-user")}

    @app.post("/api/runs/{rid}/pause")
    async def pause(rid: str) -> dict[str, Any]:
        return {"ok": await h.pause(rid)}

    @app.post("/api/runs/{rid}/resume")
    async def resume(rid: str) -> dict[str, Any]:
        try:
            await h.resume(rid)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(409, str(e))
        return run_dict(rid)

    @app.post("/api/runs/{rid}/retry")
    async def retry(rid: str) -> dict[str, Any]:
        """失败的 Run 从持久化的历史继续（attempt + 1），而不是从头再来。"""
        try:
            await h.retry(rid)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(409, str(e))
        return run_dict(rid)

    @app.post("/api/runs/{rid}/steer")
    async def steer(rid: str, body: Steer) -> dict[str, Any]:
        fn = h.follow_up if body.mode == "follow_up" else h.steer
        if not fn(rid, body.text):
            raise HTTPException(409, "Run 未在运行")
        return {"ok": True}

    @app.get("/api/runs/{rid}/trajectory")
    async def trajectory(rid: str) -> JSONResponse:
        return JSONResponse(export_trajectory(h.store, rid), headers={
            "Content-Disposition": f'attachment; filename="trajectory-{rid}.json"'})

    @app.post("/api/runs/{rid}/replay")
    async def replay(rid: str) -> dict[str, Any]:
        from ..observe.replay import replay_run

        rep = await replay_run(h.store, rid, config=h.config, base_tools=h.tools)
        return rep.to_dict()

    @app.get("/api/runs/{rid}/events")
    async def events(rid: str, request: Request, after: int = 0) -> StreamingResponse:
        run_dict(rid)
        try:
            last = int(request.headers.get("last-event-id") or after or 0)
        except ValueError:   # 坏掉的 Last-Event-ID：从头补发，不报 500
            last = 0

        async def gen():
            # 有界队列：客户端读得慢时 pump 阻塞，背压传回 EventHub（其队列满了会触发 _LAGGED → 从库补齐）
            q: asyncio.Queue = asyncio.Queue(256)

            async def pump() -> None:
                try:
                    async for ev in h.subscribe(rid, last):
                        await q.put(ev)
                except asyncio.CancelledError:
                    return  # 消费端已经退出（客户端断开）：不再放结束标记——队列满时阻塞的 put 会让任务永远卡住
                await q.put(None)  # 正常结束：消费端还在读，阻塞式放入即可（背压）

            task = asyncio.create_task(pump())
            yield "retry: 1500\n\n"
            try:
                while True:
                    try:
                        ev = await asyncio.wait_for(q.get(), 15)
                    except asyncio.TimeoutError:
                        if await request.is_disconnected():
                            break
                        yield ": ping\n\n"
                        continue
                    if ev is None:
                        yield "event: stream_end\ndata: {}\n\n"
                        break
                    yield f"id: {ev.seq}\nevent: {ev.type}\ndata: {json.dumps(ev.to_dict(), ensure_ascii=False, default=str)}\n\n"
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------------ 审批
    @app.get("/api/approvals")
    async def approvals(status: str = "pending") -> list[dict[str, Any]]:
        return [approval_dict(a) for a in h.store.list_approvals(status or None)]

    @app.post("/api/approvals/{aid}")
    async def decide(aid: str, body: ApprovalBody) -> dict[str, Any]:
        ok = h.resolve_approval(aid, body.approved, by="web-user", reason=body.reason,
                                edited_args=body.edited_args, remember=body.remember)
        if not ok:
            raise HTTPException(409, "审批已被处理（超时、取消或重复提交）")
        return {"ok": True}

    # ------------------------------------------------------------------ 静态资源
    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", RevalidatingStaticFiles(directory=WEB_DIR), name="static")
    return app
