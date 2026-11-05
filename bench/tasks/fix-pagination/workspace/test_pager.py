import unittest

from pager import paginate


class PagerTest(unittest.TestCase):
    def test_second_page_has_two_items(self):
        self.assertEqual(len(paginate([1, 2, 3, 4], 2, 2)), 2)


if __name__ == "__main__":
    unittest.main()
