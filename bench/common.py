"""基准的公共设施：方案、账本、一次性环境检查、JSONL 读写。"""

from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path
from typing import Any, Iterable, Iterator

from flywhl_harness.tools.sandbox import probe

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
PLAN = ROOT / "plan.toml"
REGRESSION = ROOT / "regression.toml"
# 失败分类沿用 tests/live/README.md：缺密钥、网络与环境、模型随机、harness 缺陷
CATEGORIES = ("missing_key", "network_env", "model_random", "harness_bug")
NOT_COUNTED = ("missing_key", "network_env")   # 这两类和被测对象无关，不进通过率的分母，但都要列出来
_ALLOWED_MOUNTS = re.compile(r"^(/|/proc(/.*)?|/sys(/.*)?|/dev(/.*)?|/etc/(resolv\.conf|hostname|hosts)|/usr/sbin/docker-init)$")


class BudgetExceeded(RuntimeError):
    """费用到了上限：停下来，已经跑完的数据都留着，补足预算后接着跑。"""


def load_plan(path: Path = PLAN) -> dict[str, Any]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if path.is_file():
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)


def append_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def unexpected_mounts(mountinfo: str) -> list[str]:
    """容器里除了系统挂载点，不应当有任何挂载：结果用 docker cp 取走，不挂宿主目录。"""
    points = [line.split()[4] for line in mountinfo.splitlines() if len(line.split()) > 4]
    return [p for p in points if not _ALLOWED_MOUNTS.match(p)]


def require_sandbox() -> None:
    """沙箱用不了时 bash 一条命令也跑不了，A 配置的失败看着像被沙箱拦住，其实什么都没测到。"""
    found = probe()
    if not found.level:
        raise SystemExit(f"系统沙箱用不了：{found.reason}")


def require_disposable() -> None:
    """真实模型会在机器上执行命令，一律在一次性容器里跑。10-31 的事故就是测试在宿主机上真的执行了载荷。"""
    in_container = Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()
    if not in_container or os.environ.get("FLYWHL_BENCH_DISPOSABLE") != "1":
        raise SystemExit("拒绝运行：真实模型的基准只在一次性容器里跑，用 bench/run.sh 启动")
    extra = unexpected_mounts(Path("/proc/self/mountinfo").read_text(encoding="utf-8"))
    if extra:
        raise SystemExit(f"拒绝运行：容器里有多余的挂载 {extra}，基准不挂宿主目录")


class Ledger:
    """费用账本：每家一个上限，总额一个上限。落盘，中断后接着算。"""

    def __init__(self, path: Path, *, total_usd: float, per_model_usd: float):
        self.path, self.total_usd, self.per_model_usd = path, total_usd, per_model_usd
        self.spent: dict[str, float] = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    @classmethod
    def from_plan(cls, plan: dict[str, Any], out: Path) -> "Ledger":
        return cls(out / "spent.json", total_usd=plan["budget"]["total_usd"], per_model_usd=plan["budget"]["per_model_usd"])

    @property
    def total(self) -> float:
        return sum(self.spent.values())

    def check(self, model: str) -> None:
        if self.total >= self.total_usd:
            raise BudgetExceeded(f"总费用 ${self.total:.4f} 已到上限 ${self.total_usd}")
        if self.spent.get(model, 0.0) >= self.per_model_usd:
            raise BudgetExceeded(f"{model} 的费用 ${self.spent[model]:.4f} 已到上限 ${self.per_model_usd}")

    def charge(self, model: str, usd: float) -> None:
        self.spent[model] = round(self.spent.get(model, 0.0) + usd, 6)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.spent, indent=2, sort_keys=True), encoding="utf-8")
