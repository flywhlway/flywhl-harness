"""把标题转成 URL 片段。"""

import re
import unicodedata


def slugify(text, max_length=None):
    """把任意标题转成 URL 片段。

    - 重音字母转成不带重音的 ASCII 字母（é -> e）；转不成 ASCII 的字符（比如汉字）直接丢掉
    - 全部转小写
    - 连续的非字母数字字符合并成一个连字符
    - 结果首尾不带连字符
    - 给了 max_length 时，结果不超过这个长度，截断后末尾也不能留连字符
    - 没有可用字符时返回空字符串
    """
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    if max_length is not None:
        slug = slug[:max_length].rstrip("-")
    return slug
