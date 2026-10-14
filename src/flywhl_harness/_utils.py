"""通用小工具：ID、时间、规范化 JSON、哈希、token 估算与文本截断。

这些函数看似琐碎，却直接决定了 harness 的几个关键性质：
- ``canonical_json``：确定性序列化。重复调用检测、输入哈希、前缀缓存稳定都依赖它。
- ``estimate_tokens``：上下文水位判断的“温度计”。只用于决策，计费永远以 provider 返回的 usage 为准。
- ``truncate_*``：工具结果截断。截断必须显式告知模型，绝不静默丢数据。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from typing import Any

_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def _base36(n: int) -> str:
    out = []
    while n:
        n, r = divmod(n, 36)
        out.append(_B36[r])
    return "".join(reversed(out)) or "0"


def new_id(prefix: str = "") -> str:
    """生成短、可读、近似按时间排序的 ID：``前缀 + 毫秒时间戳(36 进制) + 8 位随机``。

    近似单调的 ID 让日志与数据库按 ID 排序即近似按时间排序，排查问题时非常友好。
    """
    return f"{prefix}{_base36(int(time.time() * 1000))}{uuid.uuid4().hex[:8]}"


def now() -> float:
    """当前 Unix 时间戳（秒，浮点）。集中封装便于测试时替换。"""
    return time.time()


def canonical_json(obj: Any) -> str:
    """确定性 JSON：键排序、紧凑分隔符、保留中文。

    同一对象永远得到同一字符串——这是“参数签名”“输入哈希”“缓存前缀稳定”的基础。
    （Manus 的经验：JSON 键顺序不稳定就会悄悄打碎 KV-cache。）
    """
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def sha256_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def short_hash(obj: Any, n: int = 16) -> str:
    """对任意可 JSON 化对象求短哈希，用于签名与去重。"""
    text = obj if isinstance(obj, str) else canonical_json(obj)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:n]


# CJK 统一表意文字、假名、韩文、全角符号等：这些字符在主流 tokenizer 中大约 1 字 ≈ 1 token
_WIDE = re.compile(r"[⺀-鿿぀-ヿ가-힯豈-﫿＀-￯]")


def estimate_tokens(text: str | None) -> int:
    """粗略估算 token 数：宽字符按 1 token/字，其余按 4 字符/token。

    误差在 ±20% 量级，足够做“是否接近窗口上限”的判断；精确计量请使用 provider 的 usage。
    """
    if not text:
        return 0
    wide = len(_WIDE.findall(text))
    return wide + (len(text) - wide + 3) // 4


def truncate_head(text: str, max_lines: int, max_bytes: int) -> tuple[str, bool, int]:
    """保留开头（适合 read：模型通常需要从头看起）。返回 (文本, 是否截断, 总行数)。

    不会切出半行：按整行累加，直到触达行数或字节上限（借鉴 pi 的截断规则）。
    """
    lines = text.splitlines(keepends=True)
    out, size = [], 0
    for line in lines[:max_lines]:
        b = len(line.encode("utf-8"))
        if size + b > max_bytes and out:
            break
        out.append(line)
        size += b
    truncated = len(out) < len(lines)
    return "".join(out), truncated, len(lines)


def truncate_tail(text: str, max_lines: int, max_bytes: int) -> tuple[str, bool, int]:
    """保留结尾（适合 bash：错误信息与最终结果通常在最后）。返回 (文本, 是否截断, 总行数)。"""
    lines = text.splitlines(keepends=True)
    out, size = [], 0
    for line in reversed(lines[-max_lines:] if max_lines else []):
        b = len(line.encode("utf-8"))
        if size + b > max_bytes and out:
            break
        out.append(line)
        size += b
    out.reverse()
    truncated = len(out) < len(lines)
    return "".join(out), truncated, len(lines)


def head_tail_lines(text: str, head: int, tail: int, max_chars: int) -> tuple[str, int]:
    """头尾预览：保留前 head 行与后 tail 行，中间显式标注省略了多少行。返回 (预览, 总行数)。

    对日志类输出，开头说明“是什么”，结尾通常是“结果/错误”，中间才是可以按需再取的细节。
    """
    lines = text.splitlines()
    if len(lines) <= head + tail:
        out = text
    else:
        omitted = len(lines) - head - tail
        out = "\n".join(lines[:head] + [f"…（省略 {omitted} 行）…"] + lines[-tail:])
    return clip(out, max_chars), len(lines)


def clip(text: str, max_chars: int, marker: str = "…[已截断]") -> str:
    """简单的字符级截断，用于日志、摘要输入等非关键场景。"""
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - len(marker))] + marker


def safe_json_loads(raw: str) -> tuple[Any, str | None]:
    """解析 JSON，失败时返回 (None, 错误描述) 而不是抛异常——工具参数解析失败要回给模型修正。"""
    if raw is None or raw.strip() == "":
        return {}, None
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as e:
        return None, f"JSON 解析失败：{e.msg}（第 {e.lineno} 行第 {e.colno} 列）"


def extract_json_block(text: str) -> Any | None:
    """从模型输出中提取 JSON：优先 ```json 代码块，其次第一个完整的 {...}。用于子代理回传契约。"""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = [m.group(1)] if m else []
    start = text.find("{")
    if start >= 0:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start : i + 1])
                    break
    for c in candidates:
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            continue
    return None
