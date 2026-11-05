"""解析应用日志。"""

import re
from dataclasses import dataclass

LINE = re.compile(r"(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) (DEBUG|INFO|WARNING|ERROR) (.+)")


@dataclass
class Entry:
    date: str
    time: str
    level: str
    message: str


def parse_line(line):
    """解析一行日志，空行或格式不对时返回 None。"""
    match = LINE.fullmatch(line.rstrip("\n"))
    return Entry(*match.groups()) if match else None
