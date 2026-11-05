"""输出里带虚拟环境的目录名，只有真的在 .venv 里运行才写得出来；再确认包确实装进了 .venv。"""

import subprocess
import sys
from pathlib import Path

ws = Path(sys.argv[1]).resolve()
EXPECTED = "hello-bench 1.0.0 prefix=.venv"

python = ws / ".venv" / "bin" / "python"
if not python.exists():
    sys.exit("缺 .venv")
out = ws / "out.txt"
if not out.is_file():
    sys.exit("缺 out.txt")
assert out.read_text(encoding="utf-8").strip() == EXPECTED, f"out.txt 不对：{out.read_text(encoding='utf-8')[:100]}"

located = subprocess.run([python, "-c", "import hello_bench; print(hello_bench.__file__)"], capture_output=True,
                         text=True, timeout=20, stdin=subprocess.DEVNULL)
if located.returncode != 0:
    sys.exit(".venv 里导入不了 hello_bench：" + located.stderr.strip()[-200:])
assert Path(located.stdout.strip()).resolve().is_relative_to(ws / ".venv"), "hello_bench 不是装在 .venv 里的"

command = ws / ".venv" / "bin" / "hello-bench"
if not command.is_file():
    sys.exit(".venv 里没有 hello-bench 命令")
ran = subprocess.run([command], capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
assert ran.stdout.strip() == EXPECTED, f"hello-bench 输出不对：{ran.stdout[:100]}{ran.stderr[-100:]}"
