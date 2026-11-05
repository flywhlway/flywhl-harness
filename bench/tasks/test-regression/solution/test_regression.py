import unittest

from ranges import merge_ranges, total_covered


class ContainedRangeTest(unittest.TestCase):
    def test_contained_range_keeps_outer_end(self):
        self.assertEqual(merge_ranges([(0, 100), (10, 20)]), [(0, 100)])

    def test_total_covered_counts_outer_range(self):
        self.assertEqual(total_covered([(0, 100), (10, 20)]), 100)


if __name__ == "__main__":
    unittest.main()
