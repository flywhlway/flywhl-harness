import json
import subprocess
import sys
from pathlib import Path

ws = Path(sys.argv[1])


def run(*args):
    r = subprocess.run([sys.executable, "wordcount.py", *args], cwd=ws, capture_output=True, encoding="utf-8", timeout=20,
                       stdin=subprocess.DEVNULL)
    assert r.returncode == 0, f"wordcount.py {args} 退出码 {r.returncode}：{r.stderr.strip()[-200:]}"
    return r.stdout


# 文件名, 内容, 行数, 词数
SAMPLES = [
    ("check_plain.txt", "one two three\nfour five\n\nsix\n", 4, 6),
    ("check 中文 名.txt", "第一行 有 三个词\n第二行没有换行", 1, 4),
    ("check_empty.txt", "", 0, 0),
]
for name, text, lines, words in SAMPLES:
    data = text.encode("utf-8")
    (ws / name).write_bytes(data)
    want = {"lines": lines, "words": words, "bytes": len(data), "file": name}

    got = run(name)
    assert got == f"{lines} {words} {len(data)} {name}\n", f"不加 --json 时输出变了：{got!r}"

    for args in ((name, "--json"), ("--json", name)):
        out = run(*args)
        assert len(out.strip().splitlines()) == 1, f"--json 应当只输出一行：{out!r}"
        try:
            parsed = json.loads(out)
        except ValueError:
            sys.exit(f"--json 的输出不是 JSON：{out!r}")
        assert parsed == want, f"--json 输出 {parsed}，应为 {want}"
