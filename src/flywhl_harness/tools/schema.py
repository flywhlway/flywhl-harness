"""迷你 JSON Schema：参数校验 + 从函数签名推导 schema。

为什么要自己写？
- 工具参数校验是 harness 的“防呆层”：模型给的参数不合 schema 时，harness 要返回
  **可操作** 的错误（哪个字段、期望什么、实际是什么），让模型自行修正，而不是抛异常中断 Loop。
- 只实现工具调用常用的子集（type/enum/required/properties/items/范围/长度/pattern/default/anyOf），
  约 150 行，读者能完整理解；生产中可替换为 jsonschema / pydantic / TypeBox。

类型转换（coerce）：模型偶尔会把整数写成 "5"、布尔写成 "true"。pi 用 TypeBox 的 Value.Convert
做同样的宽容转换——能无歧义转换的就转，不能的再报错。
"""

from __future__ import annotations

import inspect
import json
import re
import types
import typing
from typing import Any, Literal, get_args, get_origin

_TYPE_NAMES = {
    "string": "字符串",
    "integer": "整数",
    "number": "数字",
    "boolean": "布尔值",
    "array": "数组",
    "object": "对象",
    "null": "null",
}


def _type_ok(t: str, v: Any) -> bool:
    if t == "string":
        return isinstance(v, str)
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if t == "boolean":
        return isinstance(v, bool)
    if t == "array":
        return isinstance(v, list)
    if t == "object":
        return isinstance(v, dict)
    if t == "null":
        return v is None
    return True


def _coerce(t: str, v: Any) -> tuple[bool, Any]:
    """尝试无歧义的类型转换。返回 (是否成功, 转换后的值)。"""
    if isinstance(v, str):
        s = v.strip()
        if t == "integer" and re.fullmatch(r"-?\d+", s):
            return True, int(s)
        if t == "number" and re.fullmatch(r"-?\d+(\.\d+)?([eE]-?\d+)?", s):
            return True, float(s)
        if t == "boolean" and s.lower() in ("true", "false"):
            return True, s.lower() == "true"
    if t == "number" and isinstance(v, int) and not isinstance(v, bool):
        return True, float(v)
    if t == "string" and isinstance(v, (int, float)) and not isinstance(v, bool):
        return True, str(v)
    if t in ("array", "object") and isinstance(v, str) and v.strip()[:1] in ("[", "{"):
        try:  # 模型偶尔把数组/对象当作 JSON 字符串传进来
            parsed = json.loads(v)
        except json.JSONDecodeError:
            parsed = None
        if (t == "array" and isinstance(parsed, list)) or (t == "object" and isinstance(parsed, dict)):
            return True, parsed
        return False, v
    if t == "array" and not isinstance(v, list) and v is not None and not isinstance(v, dict):
        return True, [v]  # 单值包成数组：模型常见的“只给一个”的情况
    return False, v


def validate(schema: dict[str, Any], value: Any, path: str = "$", coerce: bool = True) -> tuple[Any, list[str]]:
    """校验并（可选）转换 ``value``。返回 (新值, 错误列表)，错误为空表示通过。"""
    errors: list[str] = []

    if "anyOf" in schema or "oneOf" in schema:
        options = schema.get("anyOf") or schema.get("oneOf")
        for opt in options:
            v2, errs = validate(opt, value, path, coerce)
            if not errs:
                return v2, []
        return value, [f"{path}: 不匹配任何允许的形式"]

    t = schema.get("type")
    if t is not None:
        allowed = t if isinstance(t, list) else [t]
        if not any(_type_ok(x, value) for x in allowed):
            converted = False
            if coerce:
                for x in allowed:
                    ok, v2 = _coerce(x, value)
                    if ok:
                        value, converted = v2, True
                        break
            if not converted:
                want = " 或 ".join(_TYPE_NAMES.get(x, x) for x in allowed)
                return value, [f"{path}: 期望{want}，实际是 {type(value).__name__}（{_preview(value)}）"]

    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: 必须等于 {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: 取值 {_preview(value)} 不在允许范围 {schema['enum']}")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: 长度至少为 {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: 长度至多为 {schema['maxLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path}: 不匹配格式 {schema['pattern']}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: 不能小于 {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: 不能大于 {schema['maximum']}")

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: 至少需要 {schema['minItems']} 项")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: 至多允许 {schema['maxItems']} 项")
        if "items" in schema:
            new_items = []
            for i, item in enumerate(value):
                v2, errs = validate(schema["items"], item, f"{path}[{i}]", coerce)
                new_items.append(v2)
                errors.extend(errs)
            value = new_items

    if isinstance(value, dict):
        props: dict[str, Any] = schema.get("properties", {})
        value = dict(value)
        # 模型常把可选参数显式写成 null：视同未提供（pi 也做了同样的宽容处理）
        for k in [k for k, v in value.items() if v is None and k in props and k not in schema.get("required", [])]:
            del value[k]
        for req in schema.get("required", []):
            if req not in value:
                errors.append(f"{path}.{req}: 缺少必填参数")
        for k, sub in props.items():
            if k in value:
                v2, errs = validate(sub, value[k], f"{path}.{k}", coerce)
                value[k] = v2
                errors.extend(errs)
            elif "default" in sub:
                value[k] = sub["default"]
        extra = schema.get("additionalProperties", True)
        unknown = [k for k in value if k not in props]
        if extra is False and unknown:
            errors.append(f"{path}: 存在未知参数 {unknown}，允许的参数为 {list(props)}")
        elif isinstance(extra, dict):
            for k in unknown:
                v2, errs = validate(extra, value[k], f"{path}.{k}", coerce)
                value[k] = v2
                errors.extend(errs)
    return value, errors


def format_errors(tool_name: str, errors: list[str]) -> str:
    """把校验错误整理成给模型看的修正提示。"""
    lines = "\n".join(f"- {e}" for e in errors[:12])
    return f"工具 {tool_name} 的参数校验失败：\n{lines}\n请对照该工具的参数 schema 修正后重新调用。"


def _preview(v: Any, n: int = 40) -> str:
    s = repr(v)
    return s if len(s) <= n else s[: n - 1] + "…"


# ====================================================================== 签名 → schema
def _annotation_schema(tp: Any) -> dict[str, Any]:
    origin = get_origin(tp)
    if origin is typing.Annotated:
        base, *meta = get_args(tp)
        sch = _annotation_schema(base)
        for m in meta:
            if isinstance(m, str):
                sch["description"] = m
            elif isinstance(m, dict):
                sch.update(m)
        return sch
    if origin in (typing.Union, types.UnionType):
        args = [a for a in get_args(tp) if a is not type(None)]
        return _annotation_schema(args[0]) if len(args) == 1 else {"anyOf": [_annotation_schema(a) for a in args]}
    if origin is Literal:
        vals = list(get_args(tp))
        sch: dict[str, Any] = {"enum": vals}
        if all(isinstance(v, str) for v in vals):
            sch["type"] = "string"
        return sch
    if origin in (list, tuple, set):
        args = get_args(tp)
        return {"type": "array", "items": _annotation_schema(args[0]) if args else {}}
    if origin is dict or tp is dict:
        return {"type": "object"}
    return {
        str: {"type": "string"},
        int: {"type": "integer"},
        float: {"type": "number"},
        bool: {"type": "boolean"},
        list: {"type": "array"},
    }.get(tp, {})


def schema_from_signature(fn: Any, skip: tuple[str, ...] = ("ctx",)) -> dict[str, Any]:
    """从函数签名推导 JSON Schema。

    用 ``Annotated[类型, "描述"]`` 写参数说明；有默认值的参数为可选。例如::

        async def search(query: Annotated[str, "关键词"], limit: Annotated[int, "条数"] = 10): ...
    """
    hints = typing.get_type_hints(fn, include_extras=True)
    props: dict[str, Any] = {}
    required: list[str] = []
    for name, param in inspect.signature(fn).parameters.items():
        if name in skip or param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        sch = _annotation_schema(hints.get(name, str))
        if param.default is inspect.Parameter.empty:
            required.append(name)
        elif param.default is not None:
            sch["default"] = param.default
        props[name] = sch
    out: dict[str, Any] = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        out["required"] = required
    return out
