"""W001：行太长。"""

from . import Violation, register

MAX_LENGTH = 100


@register("W001")
def check(path, lines):
    for number, line in enumerate(lines, 1):
        text = line.rstrip("\n")
        if len(text) > MAX_LENGTH:
            yield Violation(path, number, MAX_LENGTH + 1, "W001", f"line too long ({len(text)} > {MAX_LENGTH})")
