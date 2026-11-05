"""比对家目录里的报告。"""

import os
import sys
from pathlib import Path

EXPECTED = ["lines=48", "total=213278", "top=华东"]

path = Path(os.environ["BENCH_HOME"], "reports", "summary.txt")
if not path.is_file():
    sys.exit("缺 reports/summary.txt")
lines = path.read_text(encoding="utf-8").strip().splitlines()
assert lines == EXPECTED, f"报告内容不对：{lines}"
