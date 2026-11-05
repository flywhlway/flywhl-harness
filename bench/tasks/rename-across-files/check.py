import re
import subprocess
import sys
from pathlib import Path

ws = Path(sys.argv[1])

# 先比输出：夹具原样也能走过这一步，说明下面的期望值确实是改名前的输出
r = subprocess.run([sys.executable, "main.py"], cwd=ws, capture_output=True, encoding="utf-8", timeout=20,
                   stdin=subprocess.DEVNULL)
assert r.returncode == 0, f"main.py 跑不起来：{r.stderr.strip()[-200:]}"
want = """A-1001: 4608
  subtotal: 4348
  tax: 260
  total: 4608
A-1002: 12720
  subtotal: 12000
  tax: 720
  total: 12720
"""
assert r.stdout == want, f"main.py 的输出变了：{r.stdout!r}"

# 按完整标识符搜，calc_total_tax 不算命中；__pycache__ 是运行留下的，不算工作区内容
OLD = re.compile(rb"(?<![A-Za-z0-9_])calc_total(?![A-Za-z0-9_])")
for path in ws.rglob("*"):
    if path.is_file() and "__pycache__" not in path.parts and OLD.search(path.read_bytes()):
        sys.exit(f"{path.relative_to(ws)} 里还有旧名字 calc_total")

sys.path.insert(0, str(ws))
import pricing

items = [{"price": 1000, "qty": 3}]
assert pricing.compute_total(items) == 3180, "compute_total 的结果不对"
assert pricing.calc_subtotal(items) == 3000, "calc_subtotal 应当保持原样"
assert pricing.calc_total_tax(items) == 180, "calc_total_tax 应当保持原样"

readme = (ws / "README.md").read_text(encoding="utf-8")
assert "compute_total" in readme, "README.md 要提到新名字"
assert "calc_total_tax" in readme, "README.md 里的 calc_total_tax 不该改"
