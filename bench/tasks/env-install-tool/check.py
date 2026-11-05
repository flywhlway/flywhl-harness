"""从家目录直接执行装好的命令，不走 python 解释器；参数用提示词没举例的名字。"""

import os
import subprocess
import sys
from pathlib import Path

home = Path(os.environ["BENCH_HOME"])
command = home / ".local" / "bin" / "hello"
if not command.exists():
    sys.exit("缺 .local/bin/hello")
if not os.access(command, os.X_OK):
    sys.exit("hello 没有可执行权限")


def run(*args):
    try:
        result = subprocess.run([command, *args], cwd=home, capture_output=True, text=True, timeout=15,
                                stdin=subprocess.DEVNULL)
    except OSError as e:
        sys.exit(f"不能直接执行：{e}")
    if result.returncode != 0:
        sys.exit("运行失败：" + result.stderr.strip()[-200:])
    return result.stdout.strip()


assert run() == "hello, world! (from bench tools)", f"不带参数的输出不对：{run()}"
assert run("Grace") == "hello, Grace! (from bench tools)", f"带参数的输出不对：{run('Grace')}"
