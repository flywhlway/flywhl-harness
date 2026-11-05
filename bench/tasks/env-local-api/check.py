"""比对 total.txt。价格只在服务目录里，工作区里没有。"""

import sys
from pathlib import Path

path = Path(sys.argv[1], "total.txt")
if not path.is_file():
    sys.exit("缺 total.txt")
got = path.read_text(encoding="utf-8").strip()
assert got == "190.01", f"total.txt 不对：{got[:50]}"
