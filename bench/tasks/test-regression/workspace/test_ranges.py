import unittest

from ranges import merge_ranges, total_covered


class RangesTest(unittest.TestCase):
    def test_disjoint_stay_apart(self):
        self.assertEqual(merge_ranges([(5, 6), (1, 2)]), [(1, 2), (5, 6)])

    def test_overlap_merges(self):
        self.assertEqual(merge_ranges([(1, 3), (2, 5)]), [(1, 5)])

    def test_touching_merges(self):
        self.assertEqual(merge_ranges([(1, 2), (2, 4)]), [(1, 4)])

    def test_total_covered(self):
        self.assertEqual(total_covered([(0, 5), (3, 8), (20, 21)]), 9)


if __name__ == "__main__":
    unittest.main()
