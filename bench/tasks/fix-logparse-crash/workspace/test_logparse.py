import unittest

from logparse import parse_line


class ParseLineTest(unittest.TestCase):
    def test_message_keeps_its_spaces(self):
        entry = parse_line("2026-01-05 10:12:01 INFO 服务 启动 完成\n")
        self.assertEqual(entry.date, "2026-01-05")
        self.assertEqual(entry.time, "10:12:01")
        self.assertEqual(entry.level, "INFO")
        self.assertEqual(entry.message, "服务 启动 完成")


if __name__ == "__main__":
    unittest.main()
