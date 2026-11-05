"""汇总文章的标签，给页脚用。"""


def unique_tags(articles):
    """返回所有文章用到的标签，每个标签只出现一次。"""
    tags = set()
    for article in articles:
        tags.update(article["tags"])
    return sorted(tags)


def tag_line(articles):
    """拼成页脚展示用的一行文字。"""
    return "标签：" + "、".join(unique_tags(articles))


def article_count(articles):
    return len(articles)
