import unittest

from merge import deep_merge


class DeepMergeTest(unittest.TestCase):
    def test_override_wins(self):
        self.assertEqual(deep_merge({"a": 1, "b": 2}, {"b": 3}), {"a": 1, "b": 3})

    def test_new_keys_are_added(self):
        self.assertEqual(deep_merge({"a": 1}, {"b": 2}), {"a": 1, "b": 2})

    def test_one_level_of_nesting(self):
        merged = deep_merge({"db": {"host": "a"}}, {"db": {"port": 5432}})
        self.assertEqual(merged, {"db": {"host": "a", "port": 5432}})


if __name__ == "__main__":
    unittest.main()
