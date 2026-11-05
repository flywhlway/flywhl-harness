import sys

sys.path.insert(0, sys.argv[1])
from template import render

CTX = {
    "name": "小明",
    "count": 0,
    "ratio": 1.5,
    "off": False,
    "empty": "",
    "item_2": "二号",
    "user": {"name": "Ada", "address": {"city": "上海"}},
    "path": "C:\\new\\1",
    "raw": "{{ name }}",
}

CASES = [
    ("你好，{{ name }}！", "你好，小明！"),
    ("{{name}}/{{  name  }}/{{ item_2 }}", "小明/小明/二号"),
    ("{{ name }} 和 {{ name }}", "小明 和 小明"),
    ("{{ user.name }} 住在 {{ user.address.city }}", "Ada 住在 上海"),
    ("{{ count }} {{ ratio }} {{ off }}", "0 1.5 False"),
    ('{{ missing|default:"无" }}', "无"),
    ('{{missing|default:"a b.c|d"}}', "a b.c|d"),
    ('[{{ missing|default:"" }}]', "[]"),
    ('{{ one|default:"1" }} 和 {{ two|default:"2" }}', "1 和 2"),
    ('{{ user.age|default:"未知" }}', "未知"),
    ('{{ name.first|default:"无" }}', "无"),
    ('{{ count.first|default:"无" }}', "无"),
    ('{{ name|default:"x" }}', "小明"),
    ('{{ count|default:"9" }}/[{{ empty|default:"9" }}]', "0/[]"),
    ('{{ user.address.city|default:"x" }}', "上海"),
    ("第一行 {{ name }}\n第二行 {{ user.name }}\n", "第一行 小明\n第二行 Ada\n"),
    ("{{ path }}", "C:\\new\\1"),
    ("{{ raw }}", "{{ name }}"),
    ("没有占位符，{ 单个花括号 } 保留", "没有占位符，{ 单个花括号 } 保留"),
    ("", ""),
]
for template, want in CASES:
    got = render(template, CTX)
    assert got == want, f"render({template!r}) 得到 {got!r}，应为 {want!r}"

MISSING = [
    ("{{ nope }}", "nope"),
    ("{{ user.age }}", "user.age"),
    ("{{ user.address.zip }}", "user.address.zip"),
    ("{{ name.first }}", "name.first"),
    ("{{ name.upper }}", "name.upper"),
    ("{{ count.first }}", "count.first"),
    ("{{ name }} {{ also_nope }}", "also_nope"),
]
for template, name in MISSING:
    try:
        got = render(template, CTX)
    except KeyError as e:
        assert name in str(e), f"render({template!r}) 的 KeyError 没带变量名 {name}：{e}"
        continue
    sys.exit(f"render({template!r}) 应当抛 KeyError，却返回了 {got!r}")
