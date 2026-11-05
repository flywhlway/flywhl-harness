"""比初始提交多恰好两个提交，各碰一个文件；两处改动都进了历史，工作区干净。"""

import subprocess
import sys


def git(*args):
    result = subprocess.run(["git", *args], capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        sys.exit(f"git {args[0]} 失败：{result.stderr.strip()}")
    return result.stdout.strip()


commits = git("rev-list", "--reverse", "HEAD").split()
if len(commits) != 3:
    sys.exit(f"应当比初始提交多 2 个提交，现在一共 {len(commits)} 个")
touched = sorted(git("show", "--format=", "--name-only", c).splitlines() for c in commits[1:])
assert touched == [["README.md"], ["timer.py"]], f"每个提交只能碰一个文件：{touched}"
assert len({git("log", "-1", "--format=%s", c) for c in commits[1:]}) == 2, "两个提交的说明应当各写各的"
assert "def format_ms" in git("show", "HEAD:timer.py"), "代码改动丢了"
assert "## 许可证" in git("show", "HEAD:README.md"), "文档改动丢了"
assert git("status", "--porcelain") == "", "工作区不干净"
