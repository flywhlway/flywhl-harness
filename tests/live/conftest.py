"""真实模型测试的公共设施。

- 只在这里加载项目根目录的 ``.env``：离线测试不受本机密钥影响；
- 每个测试模块用 ``pytestmark = pytest.mark.live`` 标记，默认的 ``pytest`` 不会运行它们；
- 被测模型由 ``FLYWHL_LIVE_MODEL`` 指定（默认 ``deepseek:deepseek-flash``），没有对应密钥时整组跳过；
- 每个测试的 token 与费用汇总打印在测试报告末尾，方便估算一次完整回归要花多少钱。
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from flywhl_harness import Harness, HarnessConfig, Store
from flywhl_harness.config import load_dotenv
from flywhl_harness.events import E
from flywhl_harness.llm.profiles import resolve
from flywhl_harness.policy.approvals import ApprovalDecision

ROOT = Path(__file__).resolve().parents[2]
_LEDGER: list[dict[str, Any]] = []


@pytest.fixture(scope="session", autouse=True)
def _dotenv():
    # 放在 fixture 里而不是模块顶层：普通的离线 pytest 也会导入本文件，但不应因此读到本机密钥
    load_dotenv(ROOT / ".env")


def live_model() -> str:
    return os.environ.get("FLYWHL_LIVE_MODEL", "deepseek:deepseek-flash")


def need(spec: str, key_env: str | None = None) -> str:
    """测试依赖某个模型：没有密钥就跳过，而不是失败。``key_env`` 指定从哪个环境变量取密钥。"""
    profile, _ = resolve(spec)
    if key_env and not os.environ.get(key_env):
        pytest.skip(f"未配置 {key_env}")
    if not key_env and not profile.available():
        pytest.skip(f"未配置 {profile.label} 的密钥（{' / '.join(profile.key_env)}）")
    return spec


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    d = tmp_path / "ws"
    d.mkdir()
    return d


def live_config(ws: Path, *, model: str | None = None, **over: Any) -> HarnessConfig:
    cfg = HarnessConfig.load(path=ws / "no-such-config.toml", workspace=str(ws))
    cfg.model = need(model or live_model(), over.get("api_key_env"))
    cfg.fallback_models = []                 # 测试只测指定的模型；降级链由专门的用例覆盖
    cfg.snapshots = False
    cfg.heartbeat_seconds = 0.5
    cfg.permissions.on_ask = "allow"
    for k, v in over.items():
        target = cfg
        *path, last = k.split("__")
        for part in path:
            target = getattr(target, part)
        setattr(target, last, v)
    return cfg


def live_harness(ws: Path, *, resolver=None, model: str | None = None, **over: Any) -> Harness:
    return Harness(live_config(ws, model=model, **over), store=Store(ws.parent / "harness.db"), approval_resolver=resolver)


def recorder(decide=None):
    """审批解析器：记录每一次审批请求，按 decide(rec) 决定（默认批准）。"""
    seen: list = []

    async def resolve_(rec):
        seen.append(rec)
        return decide(rec) if decide else ApprovalDecision(True, by="live-test")

    resolve_.seen = seen  # type: ignore[attr-defined]
    return resolve_


async def run(h: Harness, prompt: str, *, session_id: str | None = None, timeout: float = 240.0, label: str = ""):
    async with asyncio.timeout(timeout):
        res = await h.run(prompt, session_id=session_id)
    note(res, label or prompt[:24])
    return res


def note(res, label: str) -> None:
    u = res.usage or {}
    _LEDGER.append({"label": label, "status": res.status, "input": u.get("input_tokens", 0),
                    "output": u.get("output_tokens", 0), "cache": u.get("cache_read_tokens", 0), "cost": u.get("cost_usd", 0.0)})


def tools_used(res) -> list[str]:
    return [e.data["name"] for e in res.events if e.type == E.TOOL_START]


def transcript(h: Harness, session_id: str) -> str:
    """失败时打印给人看的对话记录（断言消息里带上它，定位问题不用再复现）。"""
    lines = []
    for m in h.log(session_id).context().messages:
        calls = "; ".join(f"{c.name}({json.dumps(c.arguments, ensure_ascii=False)[:120]})" for c in m.tool_calls)
        lines.append(f"[{m.role}{':' + m.kind if m.kind else ''}] {m.text[:300]!r} {calls}")
    return "\n".join(lines)


def pytest_terminal_summary(terminalreporter):
    if not _LEDGER:
        return
    tr = terminalreporter
    tr.section(f"真实模型用量（{live_model()}）")
    for r in _LEDGER:
        tr.write_line(f"{r['label'][:28]:<30} {r['status']:<16} in={r['input']:>7} out={r['output']:>6} "
                      f"cache={r['cache']:>7}  ${r['cost']:.5f}")
    total = sum(r["cost"] for r in _LEDGER)
    tokens = sum(r["input"] + r["output"] + r["cache"] for r in _LEDGER)
    tr.write_line(f"合计 {len(_LEDGER)} 次运行 · {tokens} tokens · ${total:.4f}")
