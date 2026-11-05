import sys

sys.path.insert(0, sys.argv[1])
from csvline import parse_line

CASES = [
    ("plain,fields,3", ["plain", "fields", "3"]),
    ('"北京,朝阳",100', ["北京,朝阳", "100"]),
    ('id,"a,b,c",end', ["id", "a,b,c", "end"]),
    ('"他说""你好""",ok', ['他说"你好"', "ok"]),
    ('""""', ['"']),
    ('"x"",""y",z', ['x","y', "z"]),
    ("a,,b", ["a", "", "b"]),
    (",a", ["", "a"]),
    ("a,", ["a", ""]),
    (",", ["", ""]),
    ('"",""', ["", ""]),
    ('"a",,"b",', ["a", "", "b", ""]),
    ('"a,b",c\n', ["a,b", "c"]),
    ('x,"y"\r\n', ["x", "y"]),
    ("", [""]),
]
for line, want in CASES:
    got = parse_line(line)
    assert got == want, f"parse_line({line!r}) 得到 {got!r}，应为 {want!r}"
