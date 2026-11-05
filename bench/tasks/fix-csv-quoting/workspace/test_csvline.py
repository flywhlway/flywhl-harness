import unittest

from csvline import parse_line


class ParseLineTest(unittest.TestCase):
    def test_plain_fields(self):
        self.assertEqual(parse_line("a,b,c"), ["a", "b", "c"])

    def test_newline_is_not_content(self):
        self.assertEqual(parse_line("a,b\n"), ["a", "b"])
        self.assertEqual(parse_line("a,b\r\n"), ["a", "b"])

    def test_blank_line_is_one_empty_field(self):
        self.assertEqual(parse_line(""), [""])
        self.assertEqual(parse_line("\n"), [""])


if __name__ == "__main__":
    unittest.main()
