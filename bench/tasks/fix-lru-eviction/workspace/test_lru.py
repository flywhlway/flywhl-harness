import unittest

from lru import LRUCache


class LRUCacheTest(unittest.TestCase):
    def test_get_returns_value_or_default(self):
        cache = LRUCache(2)
        cache.put("a", 1)
        self.assertEqual(cache.get("a"), 1)
        self.assertEqual(cache.get("b", "无"), "无")

    def test_oldest_key_is_evicted_when_full(self):
        cache = LRUCache(2)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.put("c", 3)
        self.assertNotIn("a", cache)
        self.assertEqual(len(cache), 2)

    def test_capacity_must_be_positive(self):
        with self.assertRaises(ValueError):
            LRUCache(0)


if __name__ == "__main__":
    unittest.main()
