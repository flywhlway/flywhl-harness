"""配置：一个 dataclass 树，可从 ``flywhl.toml`` 与环境变量加载。

原则：**所有限额都可配置、都有保守默认值**。限额值应按任务类型配置，而不是全局一刀切——
例如交互式问答 max_turns=20 足够，而“迁移整个仓库”这类长任务可能需要 200 轮。

示例 flywhl.toml::

    model = "deepseek:deepseek-flash"                 # <供应商>:<模型>
    fallback_models = ["dashscope:qwen3.8-flash"]     # 降级链，可以跨供应商

    [budget]
    max_turns = 40
    max_cost_usd = 0.5

    [permissions]
    mode = "default"
    rules = ["allow:bash(git status*)", "deny:bash(rm -rf*)"]

    [[hooks]]
    event = "pre_tool_use"
    matcher = "bash"
    command = "python scripts/guard.py"
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass
class ContextConfig:
    """上下文管理参数。触发条件借鉴 pi：tokens > window - reserve；同时用比例阈值兜底。"""

    context_window: int | None = None      # None = 按模型能力表
    compact_threshold: float = 0.80        # 水位超过窗口的该比例即压缩
    reserve_tokens: int = 8192             # 为下一轮输出与工具结果预留的余量
    keep_recent_tokens: int = 12000        # 压缩时至少保留最近这么多 token 的原始消息
    tool_result_max_tokens: int = 6000     # 单个工具结果超过即外置为句柄
    clear_old_tool_results: bool = True    # 水位较高时把旧的大工具结果替换为占位（最轻量的压缩）
    clear_threshold: float = 0.55          # 触发“旧结果清理”的水位
    keep_tool_results_turns: int = 3       # 最近 N 轮的工具结果永不清理
    auto_compact: bool = True


@dataclass
class BudgetConfig:
    """Run 级限额。触发软阈值注入提醒；触发硬阈值则收尾并以明确状态结束。"""

    max_turns: int = 40
    max_tool_calls: int = 200
    max_total_tokens: int | None = None
    max_cost_usd: float | None = None
    max_wall_seconds: float | None = None
    soft_ratio: float = 0.8                # 软阈值：80% 时提醒“请收尾并写进度”
    wrap_up_on_exhaust: bool = True        # 耗尽时让模型做一次无工具的总结性收尾


@dataclass
class LoopGuardConfig:
    """死循环与无进展检测。"""

    window: int = 12                       # 观察最近 N 次调用
    warn_repeats: int = 3                  # 同签名同结果重复 N 次 → 提醒
    stop_repeats: int = 5                  # 重复 N 次 → 以 loop_detected 结束
    max_consecutive_errors: int = 6        # 连续工具错误达到 N 次 → 提醒/停止
    same_file_edit_warn: int = 6           # 同一文件被编辑 N 次 → 提醒换思路


@dataclass
class PermissionConfig:
    mode: str = "default"                  # default | accept_edits | plan | bypass
    rules: list[str] = field(default_factory=list)
    on_ask: str = "wait"                   # wait（等待人审）| allow | deny（无人值守时）
    approval_timeout: float = 600.0        # 审批超时，超时默认拒绝
    strip_secret_env: bool = True          # bash 子进程剥离 *KEY*/*TOKEN*/*SECRET* 等环境变量


@dataclass
class HookConfig:
    """进程外命令 hook（协议与 Claude Code 兼容：stdin JSON，exit 2 阻断）。"""

    event: str = "pre_tool_use"
    command: str = ""
    matcher: str = "*"
    timeout: float = 30.0
    fail_closed: bool = False


@dataclass
class HarnessConfig:
    # ---- 模型 ----
    provider: str = "auto"                 # auto | deepseek | dashscope | kimi | zhipu | minimax | openrouter | openai | ollama | anthropic | mock
    model: str | None = None               # "<供应商>:<模型>"（如 deepseek:deepseek-flash）或只写模型名
    base_url: str | None = None            # 覆盖主模型供应商的地址（代理、私有部署、兼容网关）
    api_key: str | None = None             # 不建议写在文件里；优先用环境变量
    api_key_env: str | None = None         # 从指定的环境变量读密钥（例如经 OpenRouter 调 Claude：OPENROUTER_API_KEY）
    fallback_models: list[str] = field(default_factory=list)   # 降级链，每项同样可写 "<供应商>:<模型>"
    max_tokens: int = 8192
    temperature: float | None = None
    thinking: str | None = None            # None=服务默认 | enabled | disabled（按供应商翻译成各自的开关字段）
    reasoning_effort: str | None = None    # 思考强度（low / medium / high / max……，各家取值不同）
    extra_body: dict[str, Any] = field(default_factory=dict)   # 原样并入 OpenAI 兼容请求体的厂商私有参数
    request_timeout: float = 300.0
    max_retries: int = 4
    # ---- 提示词 ----
    system_prompt: str | None = None       # 完全替换内置系统提示
    append_system_prompt: str | None = None
    load_agents_md: bool = True            # 加载工作区 AGENTS.md / CLAUDE.md
    load_skills: bool = True               # 渐进披露：只把技能清单放进提示词
    # ---- 运行时 ----
    workspace: str = "."
    data_dir: str = ".flywhl"
    tool_timeout: float = 120.0
    parallel_tool_limit: int = 8
    cancel_grace_seconds: float = 5.0      # 不可中断工具在取消后的宽限期
    heartbeat_seconds: float = 2.0
    stale_after_seconds: float = 10.0      # 心跳超过该时长视为 Worker 崩溃
    snapshots: bool = True                 # 写操作前用影子 git 打工作区快照
    max_stop_blocks: int = 3               # Stop 验收最多阻止停止的次数
    verify_command: str | None = None      # Stop 验收命令（如 "pytest -q"）
    verify_todos: bool = True              # Stop 验收：计划中仍有未完成项时阻止结束
    subagent_max_depth: int = 2            # 子代理嵌套上限：2 = 子代理还能再委托一层（孙代理没有 task 工具）
    enable_demo_tools: bool = True         # 演示用的外部副作用工具（send_notification、sleep）
    # ---- 子配置 ----
    context: ContextConfig = field(default_factory=ContextConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    loop_guard: LoopGuardConfig = field(default_factory=LoopGuardConfig)
    permissions: PermissionConfig = field(default_factory=PermissionConfig)
    hooks: list[HookConfig] = field(default_factory=list)
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)

    # ------------------------------------------------------------------ 加载
    @classmethod
    def load(cls, path: str | Path | None = None, **overrides: Any) -> "HarnessConfig":
        """加载顺序（后者覆盖前者）：默认值 → flywhl.toml → 环境变量 → 显式参数。"""
        cfg = cls()
        candidates = [Path(path)] if path else [Path("flywhl.toml"), Path(".flywhl/config.toml")]
        for p in candidates:
            if p.is_file():
                with p.open("rb") as f:
                    _merge(cfg, tomllib.load(f))
                break
        env_map = {
            "FLYWHL_PROVIDER": "provider",
            "FLYWHL_MODEL": "model",
            "FLYWHL_BASE_URL": "base_url",
            "FLYWHL_WORKSPACE": "workspace",
            "FLYWHL_DATA_DIR": "data_dir",
            "FLYWHL_THINKING": "thinking",
            "FLYWHL_REASONING_EFFORT": "reasoning_effort",
            "FLYWHL_API_KEY_ENV": "api_key_env",
        }
        for env, attr in env_map.items():
            if os.environ.get(env):
                setattr(cfg, attr, os.environ[env])
        # 也接受 LLM_PRIMARY_MODEL / LLM_FALLBACK_MODEL（<provider>:<model> 写法的通用约定），FLYWHL_* 优先
        if not os.environ.get("FLYWHL_MODEL") and os.environ.get("LLM_PRIMARY_MODEL"):
            cfg.model = os.environ["LLM_PRIMARY_MODEL"]
        fallbacks = os.environ.get("FLYWHL_FALLBACK_MODELS") or os.environ.get("LLM_FALLBACK_MODEL")
        if fallbacks:
            cfg.fallback_models = [m.strip() for m in fallbacks.split(",") if m.strip()]
        if os.environ.get("FLYWHL_PERMISSION_MODE"):
            cfg.permissions.mode = os.environ["FLYWHL_PERMISSION_MODE"]
        _merge(cfg, {k: v for k, v in overrides.items() if v is not None})
        return cfg

    def data_path(self) -> Path:
        p = Path(self.data_dir)
        if not p.is_absolute():
            p = Path(self.workspace).resolve() / p
        p.mkdir(parents=True, exist_ok=True)
        return p


def read_dotenv(path: str | Path = ".env") -> dict[str, str]:
    """解析 ``KEY=VALUE`` 形式的 .env 文件（支持注释、``export`` 前缀、引号与行尾注释），不改动环境。"""
    p = Path(path)
    if not p.is_file():
        return {}
    out: dict[str, str] = {}
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()   # 行尾注释
        if key.replace("_", "").isalnum():
            out[key] = value
    return out


def load_dotenv(path: str | Path = ".env", *, override: bool = False,
                allow: Callable[[str], bool] | None = None) -> list[str]:
    """把 .env 写入环境变量，返回加载了哪些变量名（不返回值）。默认 **不覆盖** 已存在的环境变量。

    ``allow`` 限定可以加载的变量名。来源不可信的文件必须给它：工作区可能是刚克隆的仓库，
    它的 .env 只要设置 ``OPENAI_BASE_URL`` / ``HTTPS_PROXY`` 就能把你环境里的真实密钥引到第三方，
    设置 ``FLYWHL_PERMISSION_MODE=bypass`` 就能绕过审批。库代码不会隐式调用它。
    """
    loaded: list[str] = []
    for key, value in read_dotenv(path).items():
        if (key in os.environ and not override) or (allow is not None and not allow(key)):
            continue
        os.environ[key] = value
        loaded.append(key)
    return loaded


def _merge(dc: Any, data: dict[str, Any]) -> None:
    """把字典递归合并进 dataclass；未知键直接报错，避免配置拼写错误被静默忽略。"""
    names = {f.name: f for f in fields(dc)}
    for key, value in data.items():
        if key not in names:
            raise ValueError(f"未知配置项：{key}（可选：{', '.join(sorted(names))}）")
        current = getattr(dc, key)
        if is_dataclass(current) and isinstance(value, dict):
            _merge(current, value)
        elif key == "hooks" and isinstance(value, list):
            setattr(dc, key, [h if isinstance(h, HookConfig) else HookConfig(**h) for h in value])
        else:
            setattr(dc, key, value)
