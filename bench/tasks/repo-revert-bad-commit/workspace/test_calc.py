import unittest

from calc import apply_discount, split_bill, subtotal


class CalcTest(unittest.TestCase):
    def test_subtotal(self):
        self.assertEqual(subtotal([(10, 2), (3, 1)]), 23)

    def test_apply_discount(self):
        self.assertEqual(apply_discount(19.99, 10), 17.99)

    def test_split_bill(self):
        self.assertEqual(split_bill(100, 3), 33.33)
