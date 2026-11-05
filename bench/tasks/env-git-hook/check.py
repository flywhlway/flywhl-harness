"""把工作区连同 .git 复制到临时目录，在副本里真提交，验证拒绝和放行；模型的仓库不受影响。"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ws = Path(sys.argv[1])
hook = ws / ".git" / "hooks" / "pre-commit"
if not hook.is_file():
    sys.exit("缺 .git/hooks/pre-commit")
if not os.access(hook, os.X_OK):
    sys.exit("pre-commit 没有可执行权限")

with tempfile.TemporaryDirectory(prefix="bench-hook-") as tmp:
    repo = Path(tmp, "repo")
    # 没有可写的系统临时目录时 tempfile 会退回到 cwd，也就是工作区，复制时要跳过临时目录自己
    shutil.copytree(ws, repo, symlinks=True, ignore=shutil.ignore_patterns("bench-hook-*"))

    def git(*args):
        return subprocess.run(["git", "-c", "user.name=bench", "-c", "user.email=bench@example.invalid", *args],
                              cwd=repo, capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)

    def write(name, text):
        (repo / name).write_text(text, encoding="utf-8")

    def stage(name, text):
        write(name, text)
        assert git("add", "--", name).returncode == 0, f"暂存 {name} 失败"

    def unstage(name):
        assert git("reset", "-q", "--", name).returncode == 0, f"取消暂存 {name} 失败"

    def commit(message, *extra):
        return git("commit", "-q", "-m", message, *extra).returncode == 0

    stage("clean.txt", "nothing to see\n")
    assert commit("干净的提交"), "没有 TODO 的提交被拒绝了"

    stage("notes.txt", "first\nTODO: later\n")
    assert not commit("带 TODO"), "新增 TODO 的提交没有被拒绝"
    unstage("notes.txt")

    stage("my notes.txt", "fix this TODO soon\n")
    assert not commit("文件名带空格"), "文件名带空格时没有拦住 TODO"
    unstage("my notes.txt")

    stage("staged.txt", "TODO\n")
    write("staged.txt", "cleaned up\n")
    assert not commit("暂存的是 TODO"), "暂存区有 TODO，只是工作区删了，也应当拒绝"
    unstage("staged.txt")

    stage("dirty.txt", "fine\n")
    write("dirty.txt", "fine\nTODO: not staged\n")
    assert commit("工作区有 TODO"), "TODO 没有暂存，不该拒绝"

    stage("legacy.txt", "keep\nTODO: old\n")
    assert commit("历史遗留", "--no-verify"), "--no-verify 提交失败"
    stage("legacy.txt", "keep\n")
    assert commit("删掉 TODO"), "删除 TODO 行的提交不该拒绝"
