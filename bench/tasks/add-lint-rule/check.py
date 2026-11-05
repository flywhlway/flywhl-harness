import subprocess
import sys
from pathlib import Path

ws = Path(sys.argv[1])
sys.path.insert(0, str(ws))
import rules

assert "W002" in rules.RULES, "W002 没有注册到 rules.RULES"
assert rules.RULES["W002"].__module__ == "rules.trailing_space", "W002 应当在 rules/trailing_space.py 里注册"
assert "W001" in rules.RULES, "W001 不见了"


def lint(name, text):
    (ws / name).write_text(text, encoding="utf-8")
    return subprocess.run([sys.executable, "lint.py", name], cwd=ws, capture_output=True, encoding="utf-8", timeout=20,
                          stdin=subprocess.DEVNULL)


SAMPLE = "\n".join([
    "clean line",
    "trailing space  ",
    "",
    "tab at end\t",
    "   ",
    "mixed \t ",
    "中文 行尾 ",
    "x" * 120 + "  ",
    "no newline at end ",
])
WANT = [
    "check_sample.txt:2:15: W002 trailing whitespace",
    "check_sample.txt:4:11: W002 trailing whitespace",
    "check_sample.txt:5:1: W002 trailing whitespace",
    "check_sample.txt:6:6: W002 trailing whitespace",
    "check_sample.txt:7:6: W002 trailing whitespace",
    "check_sample.txt:8:101: W001 line too long (122 > 100)",
    "check_sample.txt:8:121: W002 trailing whitespace",
    "check_sample.txt:9:18: W002 trailing whitespace",
]
r = lint("check_sample.txt", SAMPLE)
got = r.stdout.splitlines()
assert got == WANT, f"lint.py 输出 {got}，应为 {WANT}；stderr：{r.stderr.strip()[-200:]}"
assert r.returncode == 1, f"有问题时退出码应为 1，实际 {r.returncode}"

r = lint("check_clean.txt", "no problem\n\n  indented line\n")
assert r.stdout == "" and r.returncode == 0, f"干净的文件不该有输出：{r.stdout!r}，退出码 {r.returncode}"
