import unittest

from wordcount import count


class CountTest(unittest.TestCase):
    def test_counts_lines_words_and_bytes(self):
        self.assertEqual(count(b"one two\nthree\n"), (2, 3, 14))

    def test_last_line_without_newline_is_not_a_line(self):
        self.assertEqual(count(b"one\ntwo"), (1, 2, 7))

    def test_bytes_are_not_characters(self):
        self.assertEqual(count("中文\n".encode()), (1, 1, 7))


if __name__ == "__main__":
    unittest.main()
