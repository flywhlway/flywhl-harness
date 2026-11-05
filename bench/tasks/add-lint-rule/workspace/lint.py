"""小 linter：python3 lint.py 文件...，有问题时退出码是 1。"""

import sys

from rules import RULES


def lint_file(path):
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    violations = []
    for rule in RULES.values():
        violations.extend(rule(path, lines))
    return sorted(violations, key=lambda v: (v.line, v.column, v.code))


def main(argv):
    found = 0
    for path in argv[1:]:
        for violation in lint_file(path):
            print(violation.format())
            found += 1
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
