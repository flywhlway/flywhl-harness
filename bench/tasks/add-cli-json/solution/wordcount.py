"""统计文件的行数、词数和字节数，用法类似 wc。"""

import argparse
import json


def count(data):
    """data 是文件的原始字节，返回 (行数, 词数, 字节数)。行数按换行符算，和 wc 一致。"""
    text = data.decode("utf-8", errors="replace")
    return data.count(b"\n"), len(text.split()), len(data)


def main(argv=None):
    parser = argparse.ArgumentParser(description="统计行数、词数和字节数")
    parser.add_argument("file", help="要统计的文件")
    parser.add_argument("--json", action="store_true", help="输出一行 JSON")
    args = parser.parse_args(argv)

    with open(args.file, "rb") as f:
        lines, words, size = count(f.read())
    if args.json:
        print(json.dumps({"lines": lines, "words": words, "bytes": size, "file": args.file}, ensure_ascii=False))
    else:
        print(f"{lines} {words} {size} {args.file}")


if __name__ == "__main__":
    main()
