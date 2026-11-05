"""用隐藏输入运行 clean.sh。输入里放了容易被 shell 写法弄坏的内容：反斜杠、星号、-n、CRLF、无换行的末行。"""

import subprocess
import sys
from pathlib import Path

HIDDEN = (b"  alpha  \n"
          b"\n"
          b"# top comment\n"
          b"   # indented comment\n"
          b"\t\n"
          b"beta   gamma\n"
          b"\talpha\t\n"
          b"C:\\temp\\new\n"
          b"* star *\n"
          b"keep # inline\n"
          b"beta   gamma  \n"
          b"-n\n"
          b"crlf line\r\n"
          b"\xe4\xb8\xad\xe6\x96\x87 \xe8\xa1\x8c\n"
          b"alpha\n"
          b"last line no newline")
EXPECTED = [b"alpha", b"beta   gamma", b"C:\\temp\\new", b"* star *", b"keep # inline", b"-n", b"crlf line",
            b"\xe4\xb8\xad\xe6\x96\x87 \xe8\xa1\x8c", b"last line no newline"]

if not Path(sys.argv[1], "clean.sh").is_file():
    sys.exit("缺 clean.sh")


def clean(data):
    return subprocess.run(["bash", "clean.sh"], cwd=sys.argv[1], input=data, capture_output=True, timeout=10)


result = clean(HIDDEN)
if result.returncode != 0:
    sys.exit("clean.sh 退出码非 0：" + result.stderr.decode("utf-8", "replace").strip()[-200:])
lines = result.stdout.split(b"\n")
if lines and lines[-1] == b"":
    lines.pop()
assert lines == EXPECTED, f"输出不对：{lines}"
assert clean(b"# only comments\n\n   \n").stdout.strip() == b"", "没有内容时应当什么都不输出"
