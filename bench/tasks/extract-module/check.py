import ast
import subprocess
import sys
from pathlib import Path

ws = Path(sys.argv[1])
MOVED = {"normalize_spaces", "slugify", "truncate"}


def output(script):
    r = subprocess.run([sys.executable, script], cwd=ws, capture_output=True, encoding="utf-8", timeout=20,
                       stdin=subprocess.DEVNULL)
    assert r.returncode == 0, f"{script} 跑不起来：{r.stderr.strip()[-200:]}"
    return r.stdout


# 先比输出：夹具原样也能走过这一步，说明下面的期望值确实是重构前的输出
APP = """/hello-world: 这是第一篇文章的正文， 内容比较长，列…
/cafe-au-lait-croissants: 短正文
/a-very-long-title-that: line one line two l…
"""
REPORT = """hello-world title=13 summary=这是第一篇…
cafe-au-lait-croissants title=25 summary=短正文
a-very-long-title-that title=55 summary=line…
"""
assert output("app.py") == APP, f"app.py 的输出变了：{output('app.py')!r}"
assert output("report.py") == REPORT, f"report.py 的输出变了：{output('report.py')!r}"


def parse(name):
    assert (ws / name).is_file(), f"没有 {name}"
    return ast.parse((ws / name).read_text(encoding="utf-8"))


def defined(module):
    return {node.name for node in module.body if isinstance(node, ast.FunctionDef)}


def imports_textutils(module):
    for node in ast.walk(module):
        if isinstance(node, ast.ImportFrom) and node.module == "textutils":
            return True
        if isinstance(node, ast.Import) and any(alias.name == "textutils" for alias in node.names):
            return True
    return False


app, report, textutils_ast = parse("app.py"), parse("report.py"), parse("textutils.py")
assert MOVED <= defined(textutils_ast), f"textutils.py 里缺 {sorted(MOVED - defined(textutils_ast))}"
assert not MOVED & defined(app), f"app.py 里还定义着 {sorted(MOVED & defined(app))}"
assert {"load_articles", "render_index", "main"} <= defined(app), "app.py 自己的函数不该移走"
assert imports_textutils(app), "app.py 应当从 textutils 导入"
assert imports_textutils(report), "report.py 应当从 textutils 导入"
for node in ast.walk(report):
    if isinstance(node, ast.ImportFrom) and node.module == "app":
        leftover = MOVED & {alias.name for alias in node.names}
        assert not leftover, f"report.py 还在从 app 导入 {sorted(leftover)}"

# 换一批脚本里没有的输入，确认函数移过去之后依然完整，也确认 textutils 不依赖 app
sys.path.insert(0, str(ws))
import textutils

assert textutils.normalize_spaces(" a\t b\n c ") == "a b c", "normalize_spaces 的行为变了"
assert textutils.slugify("Ünï çödé  Title!!") == "uni-code-title", "slugify 的行为变了"
assert textutils.slugify("x" * 40) == "x" * 23, "slugify 的长度上限变了"
assert textutils.truncate("a  b   c", 10) == "a b c", "truncate 应当先规整空白"
assert textutils.truncate("abcdefgh", 5) == "abcd…", "truncate 的行为变了"
assert textutils.truncate("y" * 25) == "y" * 19 + "…", "truncate 的默认长度变了"
# 放在最后：函数体里的延迟导入也要算
assert "app" not in sys.modules, "textutils.py 不能依赖 app.py"
