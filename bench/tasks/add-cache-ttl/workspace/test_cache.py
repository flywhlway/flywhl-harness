import unittest

from cache import Cache


class CacheTest(unittest.TestCase):
    def test_set_and_get(self):
        cache = Cache()
        cache.set("a", 1)
        self.assertEqual(cache.get("a"), 1)
        self.assertEqual(cache.get("b", "无"), "无")

    def test_stored_none_is_not_the_default(self):
        cache = Cache()
        cache.set("a", None)
        self.assertIsNone(cache.get("a", "无"))

    def test_delete(self):
        cache = Cache()
        cache.set("a", 1)
        cache.delete("a")
        cache.delete("没有这个键")
        self.assertEqual(len(cache), 0)


if __name__ == "__main__":
    unittest.main()
