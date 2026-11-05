"""拼通知邮件正文用的极简模板。"""

import re

PLACEHOLDER = re.compile(r'\{\{\s*([\w.]+)(?:\|default:"([^"]*)")?\s*\}\}')
MISSING = object()


def lookup(ctx, name):
    value = ctx
    for part in name.split("."):
        if not isinstance(value, dict) or part not in value:
            return MISSING
        value = value[part]
    return value


def render(template, ctx):
    """把 template 里的占位符换成 ctx 里的值，返回渲染后的字符串。

    支持 ``{{ name }}``、点号取嵌套的 ``{{ user.name }}``、带默认值的 ``{{ name|default:"x" }}``。
    变量不存在且没有默认值时抛 KeyError，错误里带变量名。
    """

    def replace(match):
        name, default = match.groups()
        value = lookup(ctx, name)
        if value is not MISSING:
            return str(value)
        if default is None:
            raise KeyError(name)
        return default

    return PLACEHOLDER.sub(replace, template)
