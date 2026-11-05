"""发布前脱敏。在容器里跑，原始数据不出容器，取走的只有脱敏后的副本。

基准在容器里运行，路径本来就不含宿主机信息，模型也看不到密钥，正常情况下这里什么都换不到。所以它是一道检查：
换到了东西要记进清单，有人去看为什么。脱敏改动过的轨迹，回放指纹可能不再对得上，Q3 要在脱敏之前跑。
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

TEXT_SUFFIXES = {".jsonl", ".json", ".log", ".toml", ".txt", ".md"}
PATTERNS = {
    "key": re.compile(r"\b(?:sk|pk|ghp|gho|github_pat|xox[abpr]|AKIA)[-_][A-Za-z0-9_\-]{12,}|\bAKIA[0-9A-Z]{16}\b|\bBearer\s+[A-Za-z0-9._\-]{16,}"),
    "path": re.compile(r"/(?:Users|home)/(?!bench\b)[A-Za-z0-9._-]+"),
}
SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD", re.I)


def env_secrets(env: dict[str, str] | None = None) -> list[str]:
    """环境变量里的密钥原文：格式再怪也能按原文匹配。太短的不算，否则会把普通单词替换掉。"""
    items = (env if env is not None else os.environ).items()
    return sorted({v for k, v in items if SECRET_NAME.search(k) and len(v) >= 8}, key=len, reverse=True)


def redact_text(text: str, secrets: list[str]) -> tuple[str, int]:
    hits = 0
    for value in secrets:
        n = text.count(value)
        if n:
            text, hits = text.replace(value, "<redacted:key>"), hits + n
    for name, pattern in PATTERNS.items():
        text, n = pattern.subn(f"<redacted:{name}>", text)
        hits += n
    return text, hits


def redact_tree(src: Path, dst: Path, secrets: list[str] | None = None) -> dict[str, int]:
    """把 ``src`` 脱敏复制到 ``dst``，返回 {文件: 替换次数}。认不出的文件类型不复制，宁可少发。"""
    secrets = env_secrets() if secrets is None else secrets
    hits: dict[str, int] = {}
    for path in sorted(p for p in src.rglob("*") if p.is_file()):
        rel = path.relative_to(src)
        if path.suffix not in TEXT_SUFFIXES:
            continue
        text, n = redact_text(path.read_text(encoding="utf-8", errors="replace"), secrets)
        out = dst / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        if n:
            hits[str(rel)] = n
    return hits


def leftovers(root: Path, secrets: list[str] | None = None) -> list[str]:
    """发布之前再扫一遍，查到任何东西都不发布。"""
    secrets = env_secrets() if secrets is None else secrets
    found = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        text = path.read_text(encoding="utf-8", errors="replace")
        if any(v in text for v in secrets) or any(p.search(text) for p in PATTERNS.values()):
            found.append(str(path.relative_to(root)))
    return found


def publish(raw: Path, public: Path) -> dict[str, int]:
    if public.exists():
        shutil.rmtree(public)
    hits = redact_tree(raw, public)
    bad = leftovers(public)
    if bad:
        raise SystemExit(f"脱敏后仍有疑似密钥或路径，不发布：{bad}")
    return hits
