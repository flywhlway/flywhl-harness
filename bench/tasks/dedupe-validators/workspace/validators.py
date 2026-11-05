"""注册表单的校验函数。校验通过时返回规整后的值，不通过时抛 ValidationError。"""

import re

RESERVED = {"admin", "root", "system"}


class ValidationError(ValueError):
    pass


def validate_username(value):
    """用户名：3 到 20 个字符，只有小写字母、数字和下划线。"""
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if not isinstance(value, str):
        raise ValidationError("用户名必须是字符串")
    value = value.strip()
    if not value:
        raise ValidationError("用户名不能为空")
    if len(value) < 3:
        raise ValidationError("用户名至少 3 个字符")
    if len(value) > 20:
        raise ValidationError("用户名最多 20 个字符")
    if not re.fullmatch(r"[a-z0-9_]+", value):
        raise ValidationError("用户名只能包含小写字母、数字和下划线")
    if value[0].isdigit():
        raise ValidationError("用户名不能以数字开头")
    if value[0] in "-_" or value[-1] in "-_":
        raise ValidationError("用户名首尾不能是分隔符")
    if re.search(r"[-_]{2}", value):
        raise ValidationError("用户名不能有连续的分隔符")
    if re.search(r"(.)\1\1", value):
        raise ValidationError("用户名不能有连续三个相同的字符")
    if value.startswith("tmp"):
        raise ValidationError("用户名不能以 tmp 开头")
    if value in RESERVED:
        raise ValidationError("用户名是保留字")
    return value


def validate_tag(value):
    """标签：2 到 12 个字符，不区分大小写，统一转成小写。"""
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if not isinstance(value, str):
        raise ValidationError("标签必须是字符串")
    value = value.strip().lower()
    if not value:
        raise ValidationError("标签不能为空")
    if len(value) < 2:
        raise ValidationError("标签至少 2 个字符")
    if len(value) > 12:
        raise ValidationError("标签最多 12 个字符")
    if not re.fullmatch(r"[a-z0-9-]+", value):
        raise ValidationError("标签只能包含字母、数字和连字符")
    if value[0].isdigit():
        raise ValidationError("标签不能以数字开头")
    if value[0] in "-_" or value[-1] in "-_":
        raise ValidationError("标签首尾不能是分隔符")
    if re.search(r"[-_]{2}", value):
        raise ValidationError("标签不能有连续的分隔符")
    if re.search(r"(.)\1\1", value):
        raise ValidationError("标签不能有连续三个相同的字符")
    if value.startswith("tmp"):
        raise ValidationError("标签不能以 tmp 开头")
    if value in RESERVED:
        raise ValidationError("标签是保留字")
    return value


def validate_project_code(value):
    """项目代号：2 到 8 个字符，不区分大小写，统一转成大写。"""
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if not isinstance(value, str):
        raise ValidationError("项目代号必须是字符串")
    value = value.strip().upper()
    if not value:
        raise ValidationError("项目代号不能为空")
    if not re.fullmatch(r"[A-Z0-9-]+", value):
        raise ValidationError("项目代号只能包含字母、数字和连字符")
    if len(value) < 2:
        raise ValidationError("项目代号至少 2 个字符")
    if len(value) > 8:
        raise ValidationError("项目代号不能超过 8 个字符")
    if value[0].isdigit():
        raise ValidationError("项目代号不能以数字开头")
    if value[0] in "-_" or value[-1] in "-_":
        raise ValidationError("项目代号首尾不能是分隔符")
    if re.search(r"[-_]{2}", value):
        raise ValidationError("项目代号不能有连续的分隔符")
    if re.search(r"(.)\1\1", value):
        raise ValidationError("项目代号不能有连续三个相同的字符")
    if value.lower().startswith("tmp"):
        raise ValidationError("项目代号不能以 tmp 开头")
    if value.lower() in RESERVED:
        raise ValidationError("项目代号是保留字")
    return value
