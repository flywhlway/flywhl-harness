"""检查列表页的标题和摘要，排查显示问题时用。"""

from app import load_articles
from textutils import slugify, truncate


def main():
    for article in load_articles():
        slug = slugify(article["title"])
        print(f"{slug} title={len(article['title'])} summary={truncate(article['body'], 6)}")


if __name__ == "__main__":
    main()
