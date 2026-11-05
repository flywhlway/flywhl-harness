import unittest

from slugify import slugify


class SlugifyTest(unittest.TestCase):
    def test_lowercase(self):
        self.assertEqual(slugify("Hello World"), "hello-world")

    def test_accents_become_ascii(self):
        self.assertEqual(slugify("Crème Brûlée"), "creme-brulee")

    def test_non_ascii_dropped(self):
        self.assertEqual(slugify("a中b"), "ab")

    def test_runs_of_symbols_collapse(self):
        self.assertEqual(slugify("a -- b!!c"), "a-b-c")

    def test_no_edge_hyphens(self):
        self.assertEqual(slugify("  --hi--  "), "hi")

    def test_max_length(self):
        self.assertEqual(slugify("abcdef", max_length=3), "abc")
        self.assertEqual(slugify("abc", max_length=10), "abc")

    def test_truncation_drops_trailing_hyphen(self):
        self.assertEqual(slugify("hello world", max_length=6), "hello")

    def test_empty_result(self):
        self.assertEqual(slugify("!!!"), "")
        self.assertEqual(slugify(""), "")


if __name__ == "__main__":
    unittest.main()
