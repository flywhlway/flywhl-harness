"""规则注册表。

一条规则是一个函数，接收文件路径和按行读出的内容（每行带着换行符），产出 Violation。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Violation:
    path: str
    line: int  # 行号，从 1 开始
    column: int  # 问题开始的列，从 1 开始
    code: str
    message: str

    def format(self):
        return f"{self.path}:{self.line}:{self.column}: {self.code} {self.message}"


RULES = {}


def register(code):
    """装饰器：把函数注册成规则码 code 对应的规则。"""

    def decorator(fn):
        RULES[code] = fn
        return fn

    return decorator


from . import line_length, trailing_space  # noqa: E402  规则模块要用上面的 register，放在最后导入
