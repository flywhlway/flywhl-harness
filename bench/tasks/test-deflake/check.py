"""固定的哈希种子下都要稳定通过；测试还要能抓住标签丢失，防止把断言改空。这组种子里有的让原夹具过、有的让它挂。"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ws = Path(sys.argv[1])
SEEDS = (1, 2, 3, 4, 6, 11)
EXPECTED_TESTS = 3

# 每篇文章只取第一个标签：会丢 ci，文章数不受影响
WRONG = '''
def unique_tags(articles):
    return sorted({article["tags"][0] for article in articles})


def tag_line(articles):
    return "标签：" + "、".join(unique_tags(articles))


def article_count(articles):
    return len(articles)
'''


def unittest_in(directory, seed):
    return subprocess.run([sys.executable, "-m", "unittest", "test_report"], cwd=directory, capture_output=True,
                          text=True, timeout=15, stdin=subprocess.DEVNULL,
                          env={**os.environ, "PYTHONHASHSEED": str(seed)})


for seed in SEEDS:
    result = unittest_in(ws, seed)
    if result.returncode != 0:
        sys.exit(f"PYTHONHASHSEED={seed} 时测试没过：" + result.stderr.strip()[-200:])
    ran = re.search(r"Ran (\d+) test", result.stderr)
    if ran is None or int(ran.group(1)) < EXPECTED_TESTS:
        sys.exit("测试被删了")
    if "skipped" in result.stderr or "expected failures" in result.stderr:
        sys.exit("测试被跳过了")

with tempfile.TemporaryDirectory(prefix="bench-deflake-") as tmp:
    Path(tmp, "report.py").write_text(WRONG, encoding="utf-8")
    shutil.copy(ws / "test_report.py", tmp)
    if unittest_in(tmp, SEEDS[0]).returncode == 0:
        sys.exit("断言被改空了：丢标签的实现也能通过")
