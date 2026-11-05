"""回归测试要在原始有 bug 的版本上红、在模型修好的代码上绿；修好的行为再用没举过例的输入直接断言一遍。"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ws = Path(sys.argv[1])

BUGGY = '''
def merge_ranges(ranges):
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def total_covered(ranges):
    return sum(end - start for start, end in merge_ranges(ranges))
'''

test_file = ws / "test_regression.py"
if not test_file.is_file():
    sys.exit("缺 test_regression.py")


def unittest_in(directory, *modules):
    return subprocess.run([sys.executable, "-m", "unittest", *modules], cwd=directory, capture_output=True, text=True,
                          timeout=15, stdin=subprocess.DEVNULL)


with tempfile.TemporaryDirectory(prefix="bench-regression-") as tmp:
    Path(tmp, "ranges.py").write_text(BUGGY, encoding="utf-8")
    shutil.copy(test_file, tmp)
    if unittest_in(tmp, "test_regression").returncode == 0:
        sys.exit("test_regression.py 在有 bug 的版本上也通过了，没有钉住这个问题")

result = unittest_in(ws, "test_regression", "test_ranges")
if result.returncode != 0:
    sys.exit("修好后测试没通过：" + result.stderr.strip()[-200:])

sys.path.insert(0, str(ws))
from ranges import merge_ranges, total_covered  # noqa: E402

spans = [(5, 50), (10, 12), (40, 45), (60, 70)]
assert merge_ranges(spans) == [(5, 50), (60, 70)], "被包含的区间不该缩短外层区间"
assert total_covered(spans) == 55, "覆盖长度不对"
assert merge_ranges([(30, 40), (0, 100), (99, 120), (120, 130)]) == [(0, 130)], "乱序、相接的区间没有合并对"
assert merge_ranges([]) == [], "空输入要返回空列表"
