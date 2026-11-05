import unittest

from retry import retry


class RetryTest(unittest.TestCase):
    def test_returns_result(self):
        self.assertEqual(retry(lambda: 42, 3, 1, sleep=lambda seconds: None), 42)

    def test_recovers_after_one_failure(self):
        calls = []

        def fn():
            calls.append(1)
            if len(calls) == 1:
                raise ConnectionError("连不上")
            return "ok"

        sleeps = []
        self.assertEqual(retry(fn, 3, 1, sleep=sleeps.append), "ok")
        self.assertEqual(sleeps, [1])


if __name__ == "__main__":
    unittest.main()
