"""原有 6 个提交顺序不变且内容没动，顶上多一个提交，它的改动正好是坏提交的反向；测试通过。"""

import subprocess
import sys
from pathlib import Path

ws = Path(sys.argv[1])

HISTORY = ["初始提交", "docs: 写上怎么跑测试", "feat: 加税额计算", "refactor: 整理折扣计算的取整写法",
           "feat: split_bill 拒绝负金额", "test: 补税额和负金额的用例"]
BAD = 3
GOOD_LINE = "    return round(amount * (100 - percent) / 100, 2)"
BAD_LINE = "    return round(amount * (100 - percent) // 100, 2)"


def git(*args):
    result = subprocess.run(["git", *args], capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        sys.exit(f"git {args[0]} 失败：{result.stderr.strip()}")
    return result.stdout.strip()


def changes(rev):
    lines = git("show", "--format=", "-U0", rev).splitlines()
    return [line for line in lines if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))]


commits = git("rev-list", "--reverse", "--first-parent", "HEAD").split()
subjects = [git("log", "-1", "--format=%s", c) for c in commits]
if subjects[:len(HISTORY)] != HISTORY:
    sys.exit(f"原有历史被改了：{subjects}")
if len(commits) != len(HISTORY) + 1:
    sys.exit(f"应当比原来多 1 个提交，现在多 {len(commits) - len(HISTORY)} 个")
assert changes(commits[BAD]) == ["-" + GOOD_LINE, "+" + BAD_LINE], "坏提交的内容被改过"
assert changes("HEAD") == ["-" + BAD_LINE, "+" + GOOD_LINE], f"新提交不是对坏提交的撤销：{changes('HEAD')}"
assert git("status", "--porcelain", "--untracked-files=no") == "", "还有没提交的改动"

result = subprocess.run([sys.executable, "-m", "unittest", "test_calc"], cwd=ws, capture_output=True, text=True,
                        timeout=15, stdin=subprocess.DEVNULL)
if result.returncode != 0:
    sys.exit("测试没通过：" + result.stderr.strip()[-200:])
