"""把环境配置叠加到默认配置上。"""

import copy


def deep_merge(base, override):
    """返回 base 叠加 override 之后的配置，不修改两个入参。

    两边都是字典时递归合并，其他情况用 override 的值。override 里的 None 表示删掉这个键。
    """
    result = copy.deepcopy(base)
    for key, value in override.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict):
            current = result.get(key)
            result[key] = deep_merge(current if isinstance(current, dict) else {}, value)
        else:
            result[key] = copy.deepcopy(value)
    return result
