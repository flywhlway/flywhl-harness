import unittest

from validators import ValidationError, validate_project_code, validate_tag, validate_username


class ValidatorsTest(unittest.TestCase):
    def test_valid_values_are_normalized(self):
        self.assertEqual(validate_username(" alice "), "alice")
        self.assertEqual(validate_tag("Python"), "python")
        self.assertEqual(validate_project_code("fly-2"), "FLY-2")

    def test_invalid_values_raise(self):
        with self.assertRaisesRegex(ValidationError, "用户名不能为空"):
            validate_username("  ")
        with self.assertRaisesRegex(ValidationError, "标签是保留字"):
            validate_tag("root")
        with self.assertRaisesRegex(ValidationError, "项目代号至少 2 个字符"):
            validate_project_code("a")


if __name__ == "__main__":
    unittest.main()
