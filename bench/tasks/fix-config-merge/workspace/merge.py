"""把环境配置叠加到默认配置上。"""


def deep_merge(base, override):
    """返回 base 叠加 override 之后的配置。"""
    result = base
    for key, value in override.items():
        if value is None:
            del result[key]
        elif isinstance(value, dict) and key in result:
            result[key].update(value)
        else:
            result[key] = value
    return result
