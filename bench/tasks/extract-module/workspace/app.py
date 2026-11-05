"""生成文章列表页。"""

import re
import unicodedata

ARTICLES = [
    {"title": "  Hello,   World!  ", "body": "这是第一篇文章的正文，   内容比较长，列表页只显示开头的一小段。"},
    {"title": "Café au lait & Croissants", "body": "短正文"},
    {
        "title": "A very long title that will not fit into the slug limit",
        "body": "line one\n  line two\tline three and more text",
    },
]

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


def load_articles():
    return [dict(article, title=normalize_spaces(article["title"])) for article in ARTICLES]


def render_index(articles):
    return [f"/{slugify(article['title'])}: {truncate(article['body'])}" for article in articles]


def main():
    for line in render_index(load_articles()):
        print(line)


if __name__ == "__main__":
    main()
