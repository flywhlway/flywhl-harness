"""模型的测试要放过正确实现，并且杀掉每个变异实现。实现都内嵌在这里，工作区里的 slugify.py 不参与。"""

import subprocess
import sys
import tempfile
from pathlib import Path

ORIGINAL = '''
import re
import unicodedata


def slugify(text, max_length=None):
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    if max_length is not None:
        slug = slug[:max_length].rstrip("-")
    return slug
'''


def mutate(old, new):
    assert old in ORIGINAL, f"变异点不存在：{old}"
    return ORIGINAL.replace(old, new)


MUTANTS = {
    "连续符号不合并": mutate('r"[^a-z0-9]+"', 'r"[^a-z0-9]"'),
    "截断后留下末尾连字符": mutate('slug[:max_length].rstrip("-")', "slug[:max_length]"),
    "重音字母被丢掉而不是转写": mutate('unicodedata.normalize("NFKD", text)', "text"),
}

test_file = Path(sys.argv[1], "test_slugify.py")
if not test_file.is_file():
    sys.exit("缺 test_slugify.py")
source = test_file.read_bytes()


def run_against(impl):
    with tempfile.TemporaryDirectory(prefix="bench-mutants-") as tmp:
        Path(tmp, "slugify.py").write_text(impl, encoding="utf-8")
        Path(tmp, "test_slugify.py").write_bytes(source)
        return subprocess.run([sys.executable, "-m", "unittest", "test_slugify"], cwd=tmp, capture_output=True,
                              text=True, timeout=15, stdin=subprocess.DEVNULL)


result = run_against(ORIGINAL)
if result.returncode != 0:
    sys.exit("test_slugify.py 在正确实现上没通过：" + result.stderr.strip()[-200:])
for name, impl in MUTANTS.items():
    if run_against(impl).returncode == 0:
        sys.exit(f"测试没发现这个缺陷：{name}")
