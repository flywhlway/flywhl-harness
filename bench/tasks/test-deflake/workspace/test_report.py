import unittest

from report import article_count, tag_line, unique_tags

ARTICLES = [
    {"title": "写好单元测试", "tags": ["python", "testing"]},
    {"title": "流水线提速", "tags": ["testing", "ci"]},
]


class ReportTest(unittest.TestCase):
    def test_unique_tags(self):
        self.assertEqual(unique_tags(ARTICLES), ["ci", "python", "testing"])

    def test_tag_line(self):
        self.assertEqual(tag_line(ARTICLES), "标签：ci、python、testing")

    def test_article_count(self):
        self.assertEqual(article_count(ARTICLES), 2)


if __name__ == "__main__":
    unittest.main()
