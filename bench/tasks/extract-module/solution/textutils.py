"""文本处理函数。"""

import re
import unicodedata

MAX_SLUG = 23
NON_SLUG = re.compile(r"[^a-z0-9]+")


def normalize_spaces(text):
    return " ".join(text.split())


def slugify(text):
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = NON_SLUG.sub("-", ascii_text.lower()).strip("-")
    return slug[:MAX_SLUG].rstrip("-")


def truncate(text, limit=20):
    text = normalize_spaces(text)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"
