import copy
import sys

sys.path.insert(0, sys.argv[1])
from merge import deep_merge

CASES = [
    # 多层递归合并
    ({"db": {"host": "a", "pool": {"min": 1, "max": 5, "opts": {"x": 1}}}, "debug": False},
     {"db": {"pool": {"max": 9, "opts": {"y": 2}}}, "name": "svc"},
     {"db": {"host": "a", "pool": {"min": 1, "max": 9, "opts": {"x": 1, "y": 2}}}, "debug": False, "name": "svc"}),
    ({"a": {"x": 1}}, {"a": {}}, {"a": {"x": 1}}),
    # 类型不同时直接盖掉
    ({"a": 1}, {"a": {"b": 2}}, {"a": {"b": 2}}),
    ({"a": {"b": 2}}, {"a": 1}, {"a": 1}),
    ({"a": [1]}, {"a": {"b": 2}}, {"a": {"b": 2}}),
    # 列表整体替换，列表里的字典也不合并
    ({"hosts": ["a", "b"], "rules": [{"id": 1, "on": True}]}, {"hosts": ["c"], "rules": [{"id": 2}]},
     {"hosts": ["c"], "rules": [{"id": 2}]}),
    ({"hosts": ["a"]}, {"hosts": []}, {"hosts": []}),
    # None 删键：顶层、嵌套层、整棵子树、键不存在
    ({"a": 1, "b": {"c": 2, "d": 3}}, {"a": None, "b": {"c": None}}, {"b": {"d": 3}}),
    ({"a": {"x": 1}, "b": 2}, {"a": None}, {"b": 2}),
    ({"a": 1}, {"ghost": None}, {"a": 1}),
    ({"a": {"x": 1}}, {"a": {"ghost": None}}, {"a": {"x": 1}}),
    ({}, {"new": {"keep": 1, "drop": None}}, {"new": {"keep": 1}}),
    # 假值都是普通的值
    ({"a": 1, "b": 2, "c": 3, "d": 4}, {"a": 0, "b": False, "c": "", "d": []}, {"a": 0, "b": False, "c": "", "d": []}),
]
for base, override, want in CASES:
    base_before, override_before = copy.deepcopy(base), copy.deepcopy(override)
    label = f"deep_merge({base_before!r}, {override_before!r})"
    got = deep_merge(base, override)
    assert got == want, f"{label} 得到 {got!r}，应为 {want!r}"
    assert base == base_before, f"{label} 改掉了 base：{base!r}"
    assert override == override_before, f"{label} 改掉了 override：{override!r}"
