import subprocess
import sys
from pathlib import Path

ws = Path(sys.argv[1])


def stats(name):
    r = subprocess.run([sys.executable, "stats.py", name], cwd=ws, capture_output=True, encoding="utf-8", timeout=20,
                       stdin=subprocess.DEVNULL)
    assert r.returncode == 0, f"stats.py {name} 崩溃：{r.stderr.strip()[-200:]}"
    return r.stdout.splitlines()


def expect(name, text, want):
    (ws / name).write_text(text, encoding="utf-8")
    got = stats(name)
    assert got == want, f"{name} 的输出是 {got}，应为 {want}"


# 每份日志四个级别都出现，这样不依赖“零条的级别打不打印”
got = stats("app.log")
want = ["DEBUG: 1", "ERROR: 1", "INFO: 3", "WARNING: 2", "bad lines: 6"]
assert got == want, f"app.log 的输出是 {got}，应为 {want}"
clean = "".join(f"2026-03-01 08:00:0{i} {level} 消息 {i}\n" for i, level in enumerate(("DEBUG", "INFO", "INFO", "WARNING", "ERROR")))
expect("check_clean.log", clean, ["DEBUG: 1", "ERROR: 1", "INFO: 2", "WARNING: 1", "bad lines: 0"])

# 字段数够但内容不对的行不会抛异常，也要算坏行
messy = "\n".join([
    "2026-03-01 09:00:00 ERROR level INFO appears in the message",
    "",
    "   ",
    "Traceback (most recent call last):",
    "ERROR: something went wrong today",
    "2026-03-01 09:00:01 TRACE 不存在的级别",
    "2026-03-01 09:00:02 info 级别大小写不对",
    "2026-03-01 09:00:03 INFO",
    "INFO 2026-03-01 09:00:04 字段顺序不对",
    "2026-03-01 INFO 少了 时间",
    "2026-03-01 09:00:05 DEBUG 正常的行",
    "2026-03-01 09:00:06 INFO 另一条正常的行",
    "2026-03-01 09:00:07 WARNING 最后一行没有换行符",
])
expect("check_messy.log", messy, ["DEBUG: 1", "ERROR: 1", "INFO: 1", "WARNING: 1", "bad lines: 9"])
