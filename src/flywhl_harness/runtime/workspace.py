"""Workspace：Agent 的“工作现场”。

职责
----
1. **路径约束**：所有文件工具都经 ``resolve()`` 解析路径，阻止 ``../`` 穿越与符号链接逃逸。
   注意这只是“第一道门”；真正的隔离要靠沙箱（容器 / gVisor / microVM），见教程 Workspace 一章。
2. **漂移检测**：暂停或崩溃后恢复时，比对文件清单（大小 + mtime），告诉模型“暂停期间哪些文件变了”。
3. **快照与回滚**：用一个 *影子 git 仓库*（GIT_DIR 放在 ``.flywhl/shadow.git``，工作树就是工作区）
   在每次写操作前打快照。它不碰用户自己的 ``.git``，却能提供“版本 / 回滚 / 对比”的能力——
   这与 Cline、Claude Code 的 checkpoint 思路一致。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .._utils import short_hash

IGNORED_DIRS = {
    ".git", ".flywhl", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "dist", "build", ".next", ".turbo", ".idea", "target",
}


IGNORED_FILES = {".DS_Store", "Thumbs.db"}


class PathEscape(Exception):
    """路径逃逸出工作区。"""


@dataclass
class ManifestDiff:
    added: list[str]
    removed: list[str]
    modified: list[str]

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.modified)

    def describe(self, limit: int = 20) -> str:
        parts = []
        for label, items in (("新增", self.added), ("删除", self.removed), ("修改", self.modified)):
            if items:
                shown = ", ".join(items[:limit]) + (f" 等 {len(items)} 个" if len(items) > limit else "")
                parts.append(f"{label}：{shown}")
        return "；".join(parts) or "无变化"


class Workspace:
    def __init__(self, root: str | Path, data_dir: str | Path = ".flywhl", snapshots: bool = True):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        dd = Path(data_dir)
        self.data_dir = dd if dd.is_absolute() else self.root / dd
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # 一个 Harness 可以服务多个工作区（共用数据目录）：影子仓库必须按工作区分开，
        # 否则不同工作树共用同一个 index，快照与回滚会互相覆盖
        # （只有数据目录就在本工作区根下时才用固定名字；工作区是别的工作区的父目录时也要区分开）
        own = self.data_dir.resolve().parent == self.root
        git_dir = self.data_dir / ("shadow.git" if own else f"shadow-{short_hash(str(self.root))}.git")
        self.shadow = ShadowGit(self.root, git_dir) if snapshots else None

    # ------------------------------------------------------------------ 路径
    def resolve(self, path: str, *, allow_outside: bool = False) -> Path:
        """把模型给的路径解析为绝对路径，并做逃逸检查。

        ``Path.resolve()`` 会跟随符号链接，因此“工作区内的软链接指向 /etc”也会被识别为越界。
        """
        p = Path(os.path.expanduser(path))
        if not p.is_absolute():
            p = self.root / p
        p = p.resolve()
        if not allow_outside and not self.is_inside(p):
            raise PathEscape(f"路径 {path} 解析为 {p}，位于工作区 {self.root} 之外")
        return p

    def is_inside(self, path: str | Path) -> bool:
        try:
            Path(path).resolve().relative_to(self.root)
            return True
        except ValueError:
            return False

    def rel(self, path: str | Path) -> str:
        try:
            return str(Path(path).resolve().relative_to(self.root)) or "."
        except ValueError:
            return str(path)

    # ------------------------------------------------------------------ 清单与漂移
    def manifest(self, limit: int = 5000) -> dict[str, list[int]]:
        """文件清单：相对路径 → [大小, mtime_ns]。超过 limit 个文件时截断（大仓库只做抽样对比）。"""
        out: dict[str, list[int]] = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
            for fn in sorted(f for f in filenames if f not in IGNORED_FILES):
                fp = Path(dirpath) / fn
                try:
                    st = fp.stat()
                except OSError:
                    continue
                out[str(fp.relative_to(self.root))] = [st.st_size, st.st_mtime_ns]
                if len(out) >= limit:
                    return out
        return out

    @staticmethod
    def fingerprint(manifest: dict[str, list[int]]) -> str:
        return short_hash(manifest)

    @staticmethod
    def diff_manifest(old: dict[str, list[int]], new: dict[str, list[int]]) -> ManifestDiff:
        added = sorted(set(new) - set(old))
        removed = sorted(set(old) - set(new))
        modified = sorted(k for k in set(old) & set(new) if old[k] != new[k])
        return ManifestDiff(added, removed, modified)


class ShadowGit:
    """影子 git 仓库：为工作区提供快照、对比与回滚，不影响用户自己的版本库。"""

    def __init__(self, work_tree: Path, git_dir: Path):
        self.work_tree = work_tree
        self.git_dir = git_dir
        self.enabled = shutil.which("git") is not None
        self._lock = asyncio.Lock()

    def _git(self, *args: str, check: bool = True) -> str:
        env = {
            **os.environ,
            "GIT_DIR": str(self.git_dir),
            "GIT_WORK_TREE": str(self.work_tree),
            "GIT_TERMINAL_PROMPT": "0",
        }
        cmd = ["git", "-c", "user.name=flywhl", "-c", "user.email=flywhl@localhost",
               "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false", *args]
        r = subprocess.run(cmd, cwd=self.work_tree, env=env, capture_output=True, text=True, timeout=60)
        if check and r.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} 失败：{r.stderr.strip()}")
        return r.stdout

    def _ensure(self) -> None:
        if (self.git_dir / "HEAD").exists():
            return
        self.git_dir.mkdir(parents=True, exist_ok=True)
        self._git("init", "-q")
        info = self.git_dir / "info"
        info.mkdir(exist_ok=True)
        # 影子仓库排除自身数据目录与常见大目录；工作区自己的 .gitignore 仍然生效
        (info / "exclude").write_text("\n".join(f"{d}/" for d in sorted(IGNORED_DIRS)) + "\n")

    def _snapshot_sync(self, message: str) -> str | None:
        self._ensure()
        self._git("add", "-A")
        self._git("commit", "-q", "--allow-empty", "-m", message)
        return self._git("rev-parse", "HEAD").strip()

    async def snapshot(self, message: str) -> str | None:
        """打一个快照并返回 commit sha；git 不可用时返回 None（快照是增强能力，不是硬依赖）。"""
        if not self.enabled:
            return None
        async with self._lock:
            try:
                return await asyncio.to_thread(self._snapshot_sync, message)
            except Exception:
                return None

    async def diff(self, sha: str) -> str:
        """快照 sha 与当前工作区的差异（unified diff）。"""
        if not self.enabled:
            return ""
        async with self._lock:
            def run() -> str:
                self._git("add", "-A")
                return self._git("diff", "--cached", sha, check=False)
            return await asyncio.to_thread(run)

    async def restore(self, sha: str) -> list[str]:
        """把工作区回滚到快照 sha：恢复被修改/删除的文件，删除快照之后新增的文件。返回受影响文件。"""
        async with self._lock:
            def run() -> list[str]:
                self._git("add", "-A")
                changed = [x for x in self._git("diff", "--cached", "--name-only", sha).splitlines() if x]
                added = [x for x in self._git("diff", "--cached", "--name-only", "--diff-filter=A", sha).splitlines() if x]
                self._git("checkout", sha, "--", ".")
                for rel in added:
                    p = self.work_tree / rel
                    if p.is_file():
                        p.unlink()
                self._git("add", "-A")
                self._git("commit", "-q", "--allow-empty", "-m", f"restore {sha[:10]}")
                return changed
            return await asyncio.to_thread(run)

    async def log(self, limit: int = 30) -> list[dict]:
        if not self.enabled or not (self.git_dir / "HEAD").exists():
            return []
        async with self._lock:
            out = await asyncio.to_thread(self._git, "log", f"-{limit}", "--format=%H%x09%ct%x09%s", check=False)
        rows = []
        for line in out.splitlines():
            sha, ts, msg = (line.split("\t", 2) + ["", ""])[:3]
            rows.append({"sha": sha, "ts": int(ts or 0), "message": msg})
        return rows
