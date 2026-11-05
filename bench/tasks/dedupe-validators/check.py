import ast
import sys
from pathlib import Path

ws = Path(sys.argv[1])
sys.path.insert(0, str(ws))
import validators

MAX_LINES = 59

CHARSET_USER = "用户名只能包含小写字母、数字和下划线"
CHARSET_TAG = "标签只能包含字母、数字和连字符"
CHARSET_CODE = "项目代号只能包含字母、数字和连字符"

# 行为对照表：函数名 -> (通过的输入和返回值, 不通过的输入和错误消息)
TABLE = {
    "validate_username": (
        [("  alice_01 ", "alice_01"), ("abc", "abc"), ("abcdefghij0123456789", "abcdefghij0123456789"), (b"bob_2", "bob_2")],
        [
            (123, "用户名必须是字符串"),
            (None, "用户名必须是字符串"),
            ("   ", "用户名不能为空"),
            ("ab", "用户名至少 3 个字符"),
            ("abcdefghij01234567890", "用户名最多 20 个字符"),
            ("Alice", CHARSET_USER),
            ("a-b", CHARSET_USER),
            ("!!", "用户名至少 3 个字符"),  # 长度先于字符集
            ("ABCDEFGHIJ01234567890", "用户名最多 20 个字符"),
            ("1abc", "用户名不能以数字开头"),
            ("_abc", "用户名首尾不能是分隔符"),
            ("abc_", "用户名首尾不能是分隔符"),
            ("ab__c", "用户名不能有连续的分隔符"),
            ("baaad", "用户名不能有连续三个相同的字符"),
            ("tmp_user", "用户名不能以 tmp 开头"),
            ("admin", "用户名是保留字"),
        ],
    ),
    "validate_tag": (
        [(" Python-3 ", "python-3"), ("ab", "ab"), ("abcdefghij01", "abcdefghij01"), (b"Rust", "rust")],
        [
            ([], "标签必须是字符串"),
            ("", "标签不能为空"),
            ("a", "标签至少 2 个字符"),
            ("abcdefghij012", "标签最多 12 个字符"),
            ("c++", CHARSET_TAG),
            ("a_b", CHARSET_TAG),
            ("!", "标签至少 2 个字符"),  # 长度先于字符集
            ("3d", "标签不能以数字开头"),
            ("-ab", "标签首尾不能是分隔符"),
            ("ab-", "标签首尾不能是分隔符"),
            ("a--b", "标签不能有连续的分隔符"),
            ("AAAB", "标签不能有连续三个相同的字符"),
            ("TmpFile", "标签不能以 tmp 开头"),
            ("ROOT", "标签是保留字"),
        ],
    ),
    "validate_project_code": (
        [(" fly-2 ", "FLY-2"), ("ab", "AB"), ("ABCDEFGH", "ABCDEFGH"), (b"core", "CORE")],
        [
            (1.5, "项目代号必须是字符串"),
            (" ", "项目代号不能为空"),
            ("a", "项目代号至少 2 个字符"),
            ("ABCDEFGHI", "项目代号不能超过 8 个字符"),
            ("A_B", CHARSET_CODE),
            ("!", CHARSET_CODE),  # 这个函数字符集先于长度
            ("!!!!!!!!!", CHARSET_CODE),
            ("2FLY", "项目代号不能以数字开头"),
            ("-FLY", "项目代号首尾不能是分隔符"),
            ("FLY-", "项目代号首尾不能是分隔符"),
            ("A--B", "项目代号不能有连续的分隔符"),
            ("AAAB", "项目代号不能有连续三个相同的字符"),
            ("tmp-1", "项目代号不能以 tmp 开头"),
            ("root", "项目代号是保留字"),
        ],
    ),
}

assert issubclass(validators.ValidationError, ValueError), "ValidationError 应当还是 ValueError 的子类"
for name, (accepted, rejected) in TABLE.items():
    fn = getattr(validators, name)
    for value, want in accepted:
        got = fn(value)
        assert got == want, f"{name}({value!r}) 得到 {got!r}，应为 {want!r}"
    for value, message in rejected:
        try:
            got = fn(value)
        except validators.ValidationError as e:
            assert str(e) == message, f"{name}({value!r}) 的错误消息是 {e}，应为 {message}"
            continue
        sys.exit(f"{name}({value!r}) 应当报错，却返回了 {got!r}")

# 行为对照表放在前面：夹具原样能走到这里，说明表里记的确实是重构前的行为
source = (ws / "validators.py").read_text(encoding="utf-8")
local = {path.stem for path in ws.glob("*.py")} | {path.name for path in ws.iterdir() if path.is_dir()}
for node in ast.walk(ast.parse(source)):
    if isinstance(node, ast.Import):
        names = [alias.name for alias in node.names]
    elif isinstance(node, ast.ImportFrom):
        names = [node.module or ""]
    else:
        continue
    for name in names:
        assert name.split(".")[0] not in local, f"逻辑要留在 validators.py 里，不要从 {name} 导入"
lines = len(source.splitlines())
assert lines <= MAX_LINES, f"validators.py 有 {lines} 行，上限 {MAX_LINES} 行"
