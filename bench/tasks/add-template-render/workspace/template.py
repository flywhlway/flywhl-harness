"""拼通知邮件正文用的极简模板。"""


def render(template, ctx):
    """把 template 里的占位符换成 ctx 里的值，返回渲染后的字符串。

    支持 ``{{ name }}``、点号取嵌套的 ``{{ user.name }}``、带默认值的 ``{{ name|default:"x" }}``。
    变量不存在且没有默认值时抛 KeyError，错误里带变量名。
    """
    raise NotImplementedError
