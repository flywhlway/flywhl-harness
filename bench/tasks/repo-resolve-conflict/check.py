"""HEAD 是 main 上有两个父提交的合并提交，第二父提交是 feature；settings.py 的值符合提示词的规则。"""

import subprocess
import sys


def git(*args):
    result = subprocess.run(["git", *args], capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        sys.exit(f"git {args[0]} 失败：{result.stderr.strip()}")
    return result.stdout.strip()


assert git("rev-parse", "--abbrev-ref", "HEAD") == "main", "应当停在 main 上"
parents = git("rev-list", "--parents", "-n", "1", "HEAD").split()[1:]
if len(parents) != 2:
    sys.exit("HEAD 不是合并提交")
assert parents[1] == git("rev-parse", "feature"), "合进来的不是 feature 分支"
assert git("log", "-1", "--format=%s", parents[0]) == "开放后台来源，调整超时和重试", "第一父提交不是原来的 main"

source = git("show", "HEAD:settings.py")
for marker in ("<<<<<<<", "=======", ">>>>>>>"):
    assert marker not in source, "settings.py 里还有冲突标记"
values = {}
exec(compile(source, "settings.py", "exec"), values)
assert values.get("ALLOWED_ORIGINS") == ["https://admin.example.com", "https://app.example.com",
                                         "https://partner.example.com"], "ALLOWED_ORIGINS 不对"
assert values.get("TIMEOUT_SECONDS") == 60, "TIMEOUT_SECONDS 应当取较大的值"
assert values.get("RETRIES") == 3, "RETRIES 只有 main 改过，要保留"
assert values.get("LOG_LEVEL") == "info", "LOG_LEVEL 只有 feature 加过，要保留"
assert git("status", "--porcelain") == "", "工作区不干净"
