"""W002：行尾空白。"""

from . import Violation, register


@register("W002")
def check(path, lines):
    for number, line in enumerate(lines, 1):
        text = line.rstrip("\n")
        stripped = text.rstrip(" \t")
        if stripped != text:
            yield Violation(path, number, len(stripped) + 1, "W002", "trailing whitespace")
