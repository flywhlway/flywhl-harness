import unittest

from rules import RULES


class LineLengthTest(unittest.TestCase):
    def test_long_line_is_reported(self):
        found = list(RULES["W001"]("a.py", ["x" * 101 + "\n"]))
        self.assertEqual([v.format() for v in found], ["a.py:1:101: W001 line too long (101 > 100)"])

    def test_line_at_the_limit_is_fine(self):
        self.assertEqual(list(RULES["W001"]("a.py", ["x" * 100 + "\n"])), [])


if __name__ == "__main__":
    unittest.main()
