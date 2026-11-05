"""文件系统工具：read / write / edit / ls / grep。

pi 的核心工具只有 read / write / edit / bash 四个，外加 grep / find / ls 只读组；这里沿用同样的取舍：
少而精的工具集让模型更少纠结、提示词更短、行为更可预测。

几个值得注意的工程细节：
- **截断显式化**：read 超过 2000 行或 50KB 就截断（保留开头），并告诉模型如何继续读（offset）。
- **edit 精确替换**：old_string 必须唯一匹配；不唯一时列出所有出现的行号，引导模型补充上下文。
- **过期读取保护**：文件在上次 read 之后被外部修改过，就拒绝 edit/write，要求重新读取，
  避免覆盖用户在 Agent 运行期间的并发修改。
- **details 给 UI**：write/edit 在 details 里附 unified diff，UI 渲染成彩色差异，模型只看到一句话结论。
- **沙箱里只读的位置要过人工门**：write/edit 跑在沙箱外，不拦的话，bash 写不了的 ``.git/config`` 换个工具就写进去了。
"""

from __future__ import annotations

import difflib
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .._utils import truncate_head
from ..policy.sensitive import CREDENTIAL_GLOBS, SECRET_HINT_GLOBS, sensitivity
from ..runtime.workspace import IGNORED_DIRS, IGNORED_FILES, CredentialPath, PathEscape
from . import regex_search
from .base import Effect, Tool, ToolContext, ToolResult
from .sandbox import frozen_landing, frozen_paths

if TYPE_CHECKING:
    from .confine import Confinement

MAX_LINES = 2000
MAX_BYTES = 50 * 1024
MAX_LINE_CHARS = 2000


def _is_binary(sample: bytes) -> bool:
    return b"\x00" in sample[:8192]


def _mtimes(ctx: ToolContext) -> dict[str, int]:
    return ctx.state.setdefault("file_mtimes", {})


def _check_stale(ctx: ToolContext, p: Path) -> str | None:
    """若文件在上次读取/写入之后被外部修改，返回错误信息。"""
    seen = _mtimes(ctx).get(str(p))
    if seen is not None and p.exists() and p.stat().st_mtime_ns != seen:
        return f"文件 {p.name} 在你上次读取后已被外部修改。请先重新 read，再基于最新内容修改。"
    return None


def _diff(old: str, new: str, path: str, limit: int = 400) -> str:
    lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(), f"a/{path}", f"b/{path}", lineterm=""))
    if len(lines) > limit:
        lines = lines[:limit] + [f"... 差异过长，已省略 {len(lines) - limit} 行"]
    return "\n".join(lines)


def _read_text_strict(p: Path) -> tuple[str | None, str | None]:
    """严格按 UTF-8 读取文本。返回 (文本, 错误)。修改类工具绝不能用 errors="replace" 读再写回——那会悄悄损坏文件。"""
    raw = p.read_bytes()
    if _is_binary(raw):
        return None, f"{p.name} 是二进制文件，不能按文本修改。"
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, f"{p.name} 不是 UTF-8 编码（可能是 GBK 等），为避免损坏内容拒绝修改。请先转换编码，或用 bash 处理。"


def _atomic_write(p: Path, text: str) -> None:
    """原子写入：写临时文件 → fsync → os.replace。进程在写一半时被杀，原文件要么是旧内容，要么是新内容。"""
    tmp = p.with_name(f".{p.name}.flywhl-tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    if p.exists():
        os.chmod(tmp, p.stat().st_mode)
    os.replace(tmp, p)


def _resolve(ctx: ToolContext, path: str, *, allow_outside: bool = False) -> Path:
    p = ctx.workspace.resolve(path, allow_outside=allow_outside)
    # 执行时按真实路径再查一次凭证清单：权限判定之后、真正打开之前，符号链接可能已经被换掉（TOCTOU）
    if sensitivity(p) == "deny":
        raise CredentialPath(f"{path} 实际指向凭证位置 {p}，任何模式下都不读写")
    return p


class ReadTool(Tool):
    name = "read"
    description = (
        "读取文本文件内容，输出带行号。默认最多 2000 行或 50KB；文件更长时用 offset/limit 分段读取。"
        "路径可以是相对工作区的路径或绝对路径。修改文件前应先读取。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径"},
            "offset": {"type": "integer", "minimum": 1, "description": "起始行号（从 1 开始）"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 5000, "description": "最多读取的行数"},
        },
        "required": ["path"],
        "additionalProperties": False,
    }
    effect = Effect.READ

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        p = _resolve(ctx, args["path"], allow_outside=True)  # 越界读取交给权限层裁决
        if not p.exists():
            return ToolResult.error(f"文件不存在：{args['path']}。可以先用 ls 或 grep 确认路径。")
        if p.is_dir():
            return ToolResult.error(f"{args['path']} 是目录，请用 ls 查看目录内容。")
        raw = p.read_bytes()
        if _is_binary(raw):
            return ToolResult.error(f"{args['path']} 是二进制文件（{len(raw)} 字节），无法作为文本读取。")
        text = raw.decode("utf-8", errors="replace")
        all_lines = text.splitlines()
        offset = args.get("offset", 1)
        limit = args.get("limit", MAX_LINES)
        if offset > max(len(all_lines), 1):
            return ToolResult.error(f"offset={offset} 超出文件总行数 {len(all_lines)}。")
        window = all_lines[offset - 1 : offset - 1 + limit]
        numbered = "".join(
            f"{i:>6}\t{(ln[:MAX_LINE_CHARS] + '…') if len(ln) > MAX_LINE_CHARS else ln}\n"
            for i, ln in enumerate(window, start=offset)
        )
        body, cut, _ = truncate_head(numbered, MAX_LINES, MAX_BYTES)
        shown = body.count("\n")
        end = offset + shown - 1
        _mtimes(ctx)[str(p)] = p.stat().st_mtime_ns
        footer = ""
        if cut or end < len(all_lines):
            footer = f"\n[已显示第 {offset}–{end} 行，共 {len(all_lines)} 行。继续读取请使用 offset={end + 1}]"
        return ToolResult(
            content=(body or "(空文件)\n") + footer,
            details={"path": ctx.workspace.rel(p), "start": offset, "end": end, "total_lines": len(all_lines)},
        )


class _GatedWrite(Tool):
    """沙箱里只读的位置，内容之后都会在沙箱外起作用（下一次 ``git commit`` 跑的 hook、``flywhl.toml`` 里的配置）。
    write、edit 不在沙箱里，写进去等于绕开沙箱，所以和 ``outside_sandbox`` 过同一道人工门。门放在工具上，不放在权限层：
    回放用的 ``RecordedTool`` 没有这道门，已有轨迹里的这类写入才能照原样重放。"""

    def __init__(self, confinement: Confinement | None = None):
        self.confinement = confinement   # 和 bash 共用一份。没开沙箱时没有边界可绕，不过这道门

    def human_gate(self, args: dict[str, Any], workspace: str | None = None) -> str | None:
        box, path = self.confinement, args.get("path")
        if box is None or not box.active or not workspace or not path:
            return None
        target = Path(os.path.expanduser(str(path)))
        target = target if target.is_absolute() else Path(workspace) / target
        hit = frozen_landing(target, frozen_paths(workspace, box.protected))
        if hit is None:
            return None
        return f"{os.path.relpath(hit, os.path.realpath(workspace))} 在系统沙箱里是只读的，写进去的内容之后会在沙箱外起作用"

    def _refuse_ungated(self, p: Path, ctx: ToolContext) -> ToolResult | None:
        """执行时按真实路径再查一次：判定之后软链接可能已经换了，回放和直接调用也走不到审批。没有人确认过就拒绝。"""
        gate = self.human_gate({"path": str(p)}, str(ctx.workspace.root))
        if gate and not ctx.human_approved:
            return ToolResult.error(f"{gate}。必须由人当场确认，这次调用没有经过确认：文件未改动")
        return None


class WriteTool(_GatedWrite):
    name = "write"
    description = "创建新文件或整体覆盖已有文件（会自动创建父目录）。修改已有文件的局部内容请优先使用 edit。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径（工作区内）"},
            "content": {"type": "string", "description": "完整的文件内容"},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }
    effect = Effect.WRITE

    def preview(self, args: dict[str, Any], ctx: ToolContext) -> str | None:
        try:
            p = _resolve(ctx, args["path"], allow_outside=True)
            old = (_read_text_strict(p)[0] or "") if p.exists() else ""
            return _diff(old, args.get("content", ""), ctx.workspace.rel(p))
        except Exception:
            return None

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        p = _resolve(ctx, args["path"])
        if refused := self._refuse_ungated(p, ctx):
            return refused
        if stale := _check_stale(ctx, p):
            return ToolResult.error(stale)
        old = ""
        if p.exists():
            old, err = _read_text_strict(p)
            if err:
                return ToolResult.error(err)
        created = not p.exists()
        p.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(p, args["content"])
        _mtimes(ctx)[str(p)] = p.stat().st_mtime_ns
        rel = ctx.workspace.rel(p)
        content = args["content"]
        n = len(content.encode("utf-8"))
        lines = content.count("\n") + (0 if content.endswith("\n") or not content else 1)
        return ToolResult(
            content=f"已{'创建' if created else '覆盖'} {rel}（{n} 字节，{lines} 行）",
            details={"path": rel, "created": created, "bytes": n, "diff": _diff(old, args["content"], rel)},
        )


class EditTool(_GatedWrite):
    name = "edit"
    description = (
        "精确字符串替换：把文件中的 old_string 替换为 new_string。old_string 必须与文件内容完全一致"
        "（含缩进与空白）且唯一；若需替换所有出现位置，设置 replace_all=true。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径（工作区内）"},
            "old_string": {"type": "string", "description": "要被替换的原文（需唯一匹配）"},
            "new_string": {"type": "string", "description": "替换后的新文本"},
            "replace_all": {"type": "boolean", "default": False, "description": "是否替换全部出现位置"},
        },
        "required": ["path", "old_string", "new_string"],
        "additionalProperties": False,
    }
    effect = Effect.WRITE

    def preview(self, args: dict[str, Any], ctx: ToolContext) -> str | None:
        try:
            p = _resolve(ctx, args["path"], allow_outside=True)
            text = _read_text_strict(p)[0] or ""
            old_s, new_s = args.get("old_string", ""), args.get("new_string", "")
            if not old_s or old_s not in text:
                return None
            new_text = text.replace(old_s, new_s) if args.get("replace_all") else text.replace(old_s, new_s, 1)
            return _diff(text, new_text, ctx.workspace.rel(p))
        except Exception:
            return None

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        p = _resolve(ctx, args["path"])
        if refused := self._refuse_ungated(p, ctx):
            return refused
        if not p.exists():
            return ToolResult.error(f"文件不存在：{args['path']}。新建文件请使用 write。")
        if stale := _check_stale(ctx, p):
            return ToolResult.error(stale)
        old_s, new_s = args["old_string"], args["new_string"]
        if old_s == new_s:
            return ToolResult.error("old_string 与 new_string 相同，无需修改。")
        if not old_s:
            return ToolResult.error("old_string 不能为空。")
        text, err = _read_text_strict(p)
        if err:
            return ToolResult.error(err)
        assert text is not None
        count = text.count(old_s)
        if count == 0:
            hint = ""
            first = old_s.strip().splitlines()[0].strip() if old_s.strip() else ""
            if first:
                near = [i + 1 for i, ln in enumerate(text.splitlines()) if first in ln][:5]
                if near:
                    hint = f" 首行内容在第 {near} 行附近出现，可能是缩进或空白不一致。"
            return ToolResult.error(f"在 {args['path']} 中未找到 old_string。请先 read 确认精确内容。{hint}")
        if count > 1 and not args.get("replace_all"):
            lines = [text[: m.start()].count("\n") + 1 for m in re.finditer(re.escape(old_s), text)][:10]
            return ToolResult.error(
                f"old_string 在文件中出现 {count} 次（第 {lines} 行）。请补充更多上下文使其唯一，或设置 replace_all=true。"
            )
        new_text = text.replace(old_s, new_s) if args.get("replace_all") else text.replace(old_s, new_s, 1)
        _atomic_write(p, new_text)
        _mtimes(ctx)[str(p)] = p.stat().st_mtime_ns
        rel = ctx.workspace.rel(p)
        edits = ctx.state.setdefault("edit_counts", {})
        edits[rel] = edits.get(rel, 0) + 1
        return ToolResult(
            content=f"已修改 {rel}（替换 {count if args.get('replace_all') else 1} 处）",
            details={"path": rel, "replacements": count if args.get("replace_all") else 1, "diff": _diff(text, new_text, rel)},
        )


class LsTool(Tool):
    name = "ls"
    description = "列出目录内容（目录以 / 结尾，附文件大小）。自动跳过 .git、node_modules 等目录。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "default": ".", "description": "目录路径"},
            "depth": {"type": "integer", "minimum": 1, "maximum": 4, "default": 1, "description": "递归深度"},
        },
        "additionalProperties": False,
    }
    effect = Effect.READ

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        root = _resolve(ctx, args.get("path", "."), allow_outside=True)
        if not root.is_dir():
            return ToolResult.error(f"{args.get('path')} 不是目录或不存在。")
        depth = args.get("depth", 1)
        lines: list[str] = []
        limit = 500
        truncated = False

        def walk(d: Path, level: int, prefix: str) -> None:
            nonlocal truncated
            try:
                items = sorted(d.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
            except PermissionError:
                return
            for it in items:
                if it.name in IGNORED_FILES or (it.is_dir() and it.name in IGNORED_DIRS):
                    continue
                if len(lines) >= limit:
                    truncated = True  # 确实还有未列出的条目，才提示截断
                    return
                if it.is_dir():
                    if it.name in IGNORED_DIRS:
                        continue
                    lines.append(f"{prefix}{it.name}/")
                    if level < depth:
                        walk(it, level + 1, prefix + "  ")
                else:
                    try:
                        size = it.stat().st_size
                    except OSError:
                        size = 0
                    lines.append(f"{prefix}{it.name}  ({_human(size)})")

        walk(root, 1, "")
        more = f"\n[条目过多，仅显示前 {limit} 项；可指定子目录或减小 depth]" if truncated else ""
        return ToolResult(content=("\n".join(lines) or "(空目录)") + more, details={"path": ctx.workspace.rel(root), "count": len(lines)})


class GrepTool(Tool):
    name = "grep"
    description = (
        "在文件中按正则表达式搜索，返回 路径:行号: 内容。可用 glob 过滤文件（如 *.py）。"
        "先用 grep 定位，再用 read 查看上下文，比整文件读取省得多。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "正则表达式"},
            "path": {"type": "string", "default": ".", "description": "搜索根目录或文件"},
            "glob": {"type": "string", "description": "文件名过滤，如 *.py"},
            "ignore_case": {"type": "boolean", "default": False},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
        },
        "required": ["pattern"],
        "additionalProperties": False,
    }
    effect = Effect.READ

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        flags = re.I if args.get("ignore_case") else 0
        try:
            re.compile(args["pattern"], flags)   # 先在本进程编译一次：写错的正则立即报错，不必起子进程
        except re.error as e:
            return ToolResult.error(f"正则表达式无效：{e}。如需匹配字面量，请转义特殊字符。")
        root = _resolve(ctx, args.get("path", "."), allow_outside=True)
        cap = args.get("max_results", 100)
        try:
            # 遍历、读文件、匹配都在子进程里做（见 tools/regex_search.py）。权限层只看得到遍历的根：
            # grep -r 扫过 ~/.kube/config、项目 .env 时逐个文件拦下，要看须显式指定路径走审批
            found = await regex_search.grep_files(
                args["pattern"], flags, str(root), glob=args.get("glob"), cap=cap,
                withhold=(*CREDENTIAL_GLOBS, *SECRET_HINT_GLOBS), ignored_dirs=IGNORED_DIRS, ignored_files=IGNORED_FILES,
                timeout=min(regex_search.TIMEOUT, ctx.time_left) if ctx.time_left else None)
        except regex_search.RegexTimeout as e:
            return ToolResult.error(f"搜索超过 {e.seconds:.0f} 秒被终止：正则可能存在灾难性回溯（如 (a+)+$），"
                                    "或者搜索范围太大。请简化表达式，或用 path / glob 缩小范围。")
        hits = [f"{ctx.workspace.rel(p)}:{i}: {line}" for p, i, line in found["hits"]]
        files_scanned, withheld = found["scanned"], found["withheld"]
        note =(f"\n[跳过 {withheld} 个凭证或密钥文件：.env 这类文件可以把 path 指向它、经人工确认后搜索；"
                "~/.ssh、~/.kube 这类凭证任何模式下都不读取]") if withheld else ""
        if not hits:
            return ToolResult(content=f"未找到匹配（扫描了 {files_scanned} 个文件）。{note}",
                              details={"count": 0, "withheld": withheld})
        more = f"\n[已达到 max_results={cap}，可能还有更多匹配；请缩小范围]" if len(hits) >= cap else ""
        return ToolResult(content="\n".join(hits) + more + note,
                          details={"count": len(hits), "files_scanned": files_scanned, "withheld": withheld})


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f}TB"


__all__ = ["ReadTool", "WriteTool", "EditTool", "LsTool", "GrepTool", "PathEscape"]
