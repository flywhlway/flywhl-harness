"""解析导出文件的行。导出文件每条记录占一行，字段里不会有换行。"""


def parse_line(line):
    """把一行文本切成字段列表，行尾的换行符不算内容。"""
    text = line.rstrip("\r\n")
    fields = []
    current = []
    in_quotes = False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_quotes:
            if ch != '"':
                current.append(ch)
            elif text[i + 1 : i + 2] == '"':
                current.append('"')
                i += 1
            else:
                in_quotes = False
        elif ch == '"':
            in_quotes = True
        elif ch == ",":
            fields.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    fields.append("".join(current))
    return fields
