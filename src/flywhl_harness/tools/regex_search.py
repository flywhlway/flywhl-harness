"""正则搜索放进子进程执行：grep 与 search_ref 共用。

Python 的 ``re`` 没有超时、也不能被打断：``(a+)+$`` 这类会灾难性回溯的表达式碰上一行 ``aaaa…!``，
能把调用它的线程卡上几分钟到几小时。早期版本在事件循环里直接匹配，一个坏正则（模型写错，或者被提示注入诱导）
就让整个服务停摆——所有会话的 SSE、审批、取消都没有响应。这里把遍历目录、读文件和匹配都交给子进程：
超时或 Run 被取消时直接杀掉，事件循环始终可用。

子进程只用标准库：本文件按路径以 ``python -I -S`` 执行，不导入 flywhl_harness，也不受工作区里的同名模块和
``PYTHON*`` 环境变量影响。敏感文件清单等参数由父进程随请求传入。
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import re
import sys
from typing import Any, Iterable

TIMEOUT = 30.0                    # 单次搜索的上限：超过多半是灾难性回溯，或者搜索范围太大
MAX_FILE_BYTES = 2 * 1024 * 1024  # 跳过大文件（与 read 的定位一致：先用 grep 定位，再分段 read）
LINE_CHARS = 300


class RegexTimeout(Exception):
    def __init__(self, seconds: float):
        super().__init__(f"正则搜索超过 {seconds:.0f} 秒")
        self.seconds = seconds


async def grep_files(pattern: str, flags: int, root: str, *, glob: str | None, cap: int, withhold: Iterable[str],
                     ignored_dirs: Iterable[str], ignored_files: Iterable[str], timeout: float | None = None) -> dict:
    """在 root（目录或单个文件）下搜索。返回 ``{"hits": [[路径, 行号, 行], ...], "scanned": 扫描文件数, "withheld": 跳过的敏感文件数}``。
    遍历时按真实路径匹配 ``withhold`` 清单（不区分大小写）的文件跳过不读；root 本身是文件时不检查（权限层已经判过）。"""
    return await _run({"op": "files", "pattern": pattern, "flags": flags, "root": root, "glob": glob, "cap": cap,
                       "withhold": list(withhold), "ignored_dirs": sorted(ignored_dirs),
                       "ignored_files": sorted(ignored_files), "max_bytes": MAX_FILE_BYTES, "line_chars": LINE_CHARS},
                      timeout)


async def grep_text(pattern: str, text: str, *, flags: int = 0, cap: int = 50, timeout: float | None = None) -> dict:
    """在一段文本里逐行搜索。返回 ``{"hits": [[行号, 行], ...], "total": 匹配总数}``（hits 最多 cap 条）。"""
    return await _run({"op": "text", "pattern": pattern, "flags": flags, "text": text, "cap": cap,
                       "line_chars": LINE_CHARS}, timeout)


async def _run(request: dict[str, Any], timeout: float | None) -> dict:
    seconds = TIMEOUT if timeout is None else timeout
    proc = await asyncio.create_subprocess_exec(
        sys.executable or "python3", "-I", "-S", os.path.abspath(__file__),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(json.dumps(request).encode()), seconds)
    except asyncio.TimeoutError:
        raise RegexTimeout(seconds) from None
    finally:
        if proc.returncode is None:   # 超时或 Run 被取消：杀掉子进程，不留下一直占着 CPU 的回溯
            proc.kill()
            await proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(err.decode("utf-8", "replace").strip().splitlines()[-1:] or "正则搜索子进程异常退出")
    return json.loads(out)


# ---------------------------------------------------------------------- 子进程
def _search_files(req: dict, rx: re.Pattern) -> dict:
    hits: list[list] = []
    scanned = withheld = 0
    cap, limit, width = req["cap"], req["max_bytes"], req["line_chars"]
    root, glob = req["root"], req["glob"]
    skip_dirs, skip_files = set(req["ignored_dirs"]), set(req["ignored_files"])

    def files():
        if os.path.isfile(root):
            yield root, False
            return
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in skip_dirs)
            for fn in sorted(f for f in filenames if f not in skip_files):
                yield os.path.join(dirpath, fn), True

    for path, check in files():
        if glob and not fnmatch.fnmatch(os.path.basename(path), glob):
            continue
        if check:
            real = os.path.realpath(path).lower()
            if any(fnmatch.fnmatchcase(real, g) for g in req["withhold"]):
                withheld += 1
                continue
        try:
            with open(path, "rb") as f:
                data = f.read(limit + 1)
        except OSError:
            continue
        if len(data) > limit or b"\x00" in data[:8192]:
            continue
        scanned += 1
        for i, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
            if rx.search(line):
                hits.append([path, i, line.strip()[:width]])
                if len(hits) >= cap:
                    return {"hits": hits, "scanned": scanned, "withheld": withheld}
    return {"hits": hits, "scanned": scanned, "withheld": withheld}


def _search_text(req: dict, rx: re.Pattern) -> dict:
    hits: list[list] = []
    total = 0
    for i, line in enumerate(req["text"].splitlines(), 1):
        if rx.search(line):
            total += 1
            if len(hits) < req["cap"]:
                hits.append([i, line[:req["line_chars"]]])
    return {"hits": hits, "total": total}


def _main() -> None:
    req = json.loads(sys.stdin.buffer.read())
    rx = re.compile(req["pattern"], req["flags"])
    result = _search_files(req, rx) if req["op"] == "files" else _search_text(req, rx)
    sys.stdout.write(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    _main()
