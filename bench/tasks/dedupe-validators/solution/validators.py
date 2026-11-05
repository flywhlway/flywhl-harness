"""注册表单的校验函数。校验通过时返回规整后的值，不通过时抛 ValidationError。"""

import re

RESERVED = {"admin", "root", "system"}


class ValidationError(ValueError):
    pass


def _validate(value, label, normalize, low, high, pattern, charset, too_long="最多", charset_first=False):
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if not isinstance(value, str):
        raise ValidationError(f"{label}必须是字符串")
    value = normalize(value)
    if not value:
        raise ValidationError(f"{label}不能为空")
    if charset_first and not re.fullmatch(pattern, value):
        raise ValidationError(f"{label}只能包含{charset}")
    if len(value) < low:
        raise ValidationError(f"{label}至少 {low} 个字符")
    if len(value) > high:
        raise ValidationError(f"{label}{too_long} {high} 个字符")
    if not re.fullmatch(pattern, value):
        raise ValidationError(f"{label}只能包含{charset}")
    if value[0].isdigit():
        raise ValidationError(f"{label}不能以数字开头")
    if value[0] in "-_" or value[-1] in "-_":
        raise ValidationError(f"{label}首尾不能是分隔符")
    if re.search(r"[-_]{2}", value):
        raise ValidationError(f"{label}不能有连续的分隔符")
    if re.search(r"(.)\1\1", value):
        raise ValidationError(f"{label}不能有连续三个相同的字符")
    if value.lower().startswith("tmp"):
        raise ValidationError(f"{label}不能以 tmp 开头")
    if value.lower() in RESERVED:
        raise ValidationError(f"{label}是保留字")
    return value


def validate_username(value):
    """用户名：3 到 20 个字符，只有小写字母、数字和下划线。"""
    return _validate(value, "用户名", str.strip, 3, 20, r"[a-z0-9_]+", "小写字母、数字和下划线")


def validate_tag(value):
    """标签：2 到 12 个字符，不区分大小写，统一转成小写。"""
    return _validate(value, "标签", lambda v: v.strip().lower(), 2, 12, r"[a-z0-9-]+", "字母、数字和连字符")


def validate_project_code(value):
    """项目代号：2 到 8 个字符，不区分大小写，统一转成大写。"""
    return _validate(value, "项目代号", lambda v: v.strip().upper(), 2, 8, r"[A-Z0-9-]+", "字母、数字和连字符",
                     too_long="不能超过", charset_first=True)
