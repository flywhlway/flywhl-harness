"""解析应用日志。"""

from dataclasses import dataclass


@dataclass
class Entry:
    date: str
    time: str
    level: str
    message: str


def parse_line(line):
    date, time, level, message = line.rstrip("\n").split(" ", 3)
    return Entry(date, time, level, message)
