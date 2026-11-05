"""解析导出文件的行。导出文件每条记录占一行，字段里不会有换行。"""


def parse_line(line):
    """把一行文本切成字段列表，行尾的换行符不算内容。"""
    return line.rstrip("\r\n").split(",")
