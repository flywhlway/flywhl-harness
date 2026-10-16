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

启动目录的配置文件可能来自刚克隆的仓库：上例中的 allow 规则与 ``[[hooks]]`` 这类能执行命令、改变请求去向、
放宽审批的设置，要先信任这份文件的当前内容才会生效，见 ``split_untrusted``。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import tomllib
import warnings
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
    sandbox: str = "off"                   # off | macos（bash 跑在 sandbox-exec 里，原型，见 tools/sandbox.py）


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
    def load(cls, path: str | Path | None = None, *, trust: Callable[[TrustRequest], bool] | None = None,
             **overrides: Any) -> "HarnessConfig":
        """加载顺序（后者覆盖前者）：默认值 → 用户配置 → 项目配置 → 环境变量 → 显式参数。

        - 用户配置 ``~/.flywhl/config.toml``（目录见 ``flywhl_home()``）与 ``path`` 显式指定的文件视为可信；
        - 不给 ``path`` 时从启动目录找 ``flywhl.toml`` / ``.flywhl/config.toml``。其中的高危设置只在这份文件的
          当前内容被信任过之后生效；未信任时交给 ``trust`` 回调决定（返回 True 即记入 ``trusted.json``），
          没有回调则忽略这些设置并发出 ``UntrustedConfigWarning``；
        - 两层文件里的 ``hooks`` 与 ``permissions.rules`` 是追加而不是替换：项目配置删不掉用户配置里的守卫 hook 与 deny 规则。
        """
        cfg = cls()
        user_file = (flywhl_home() / "config.toml").resolve()
        if user_file.is_file():
            _merge(cfg, tomllib.loads(user_file.read_text(encoding="utf-8")), layer=True)
        project = Path(path) if path else next((p for p in map(Path, PROJECT_CONFIG_FILES) if p.is_file()), None)
        if project is not None and project.is_file() and project.resolve() != user_file:
            raw = project.read_bytes()      # 只读一次：哈希与解析的必须是同一份内容
            data = tomllib.loads(raw.decode("utf-8"))
            if not path:
                data = _trusted_part(project.resolve(), raw, data, trust)
            _merge(cfg, data, layer=True)
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


# ---------------------------------------------------------------------- 项目配置信任
PROJECT_CONFIG_FILES = ("flywhl.toml", ".flywhl/config.toml")

# 和启动目录的 .env 一样用白名单：只影响“用哪家模型、花多少钱、何时收尾”的设置直接生效，最坏是效果差一点、多花一点钱。
# 其余字段都要信任——hooks / mcp_servers / verify_command 会执行命令；base_url / api_key / api_key_env / extra_body
# 决定请求发往哪里、记在谁的账上；workspace / data_dir 决定 Agent 在哪里读写。新增字段默认落在需要信任的一侧。
_SAFE_KEYS = frozenset({
    "provider", "model", "fallback_models", "max_tokens", "temperature", "thinking", "reasoning_effort",
    "request_timeout", "max_retries", "system_prompt", "append_system_prompt", "load_agents_md", "load_skills",
    "tool_timeout", "parallel_tool_limit", "cancel_grace_seconds", "heartbeat_seconds", "stale_after_seconds",
    "snapshots", "max_stop_blocks", "verify_todos", "subagent_max_depth", "enable_demo_tools",
    "context", "budget", "loop_guard",
})
# permissions 按取值判断：收紧审批的取值直接生效，放宽的（bypass、accept_edits、on_ask=allow、不剥离密钥）要信任
_SAFE_PERMISSIONS: dict[str, tuple[Any, ...]] = {
    "mode": ("default", "plan"), "on_ask": ("wait", "deny"), "strip_secret_env": (True,), "sandbox": ("macos",),
}
_TIGHTENING_RULE = re.compile(r"^\s*(deny|ask)\s*:")


class UntrustedConfigWarning(UserWarning):
    """项目配置里的高危设置因为没有被信任而被忽略。"""


@dataclass
class TrustRequest:
    """一份等待信任的项目配置，交给 ``HarnessConfig.load(trust=...)`` 的回调判断。"""

    path: Path          # 绝对路径。信任绑定位置：同样一行 python scripts/guard.py，换个仓库执行的就是另一份脚本
    digest: str         # 文件内容的 SHA-256，内容一变就要重新确认
    risky: list[str]    # 需要信任才能生效的设置，每项一行，供人审阅


def flywhl_home() -> Path:
    """用户级目录：用户配置 config.toml 与信任记录 trusted.json 所在处。``FLYWHL_HOME`` 可改（测试、容器）。"""
    return Path(os.environ.get("FLYWHL_HOME") or "~/.flywhl").expanduser()


def split_untrusted(data: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """把来自不可信位置的配置拆成 (可以直接生效的部分, 需要信任的设置)。

    未知键留在前一部分，交给 ``_merge`` 照常报错——拼写错误不该变成一次信任确认。
    """
    known = {f.name for f in fields(HarnessConfig)}
    safe: dict[str, Any] = {}
    risky: list[str] = []
    for key, value in data.items():
        if key == "permissions" and isinstance(value, dict):
            perms: dict[str, Any] = {}
            for k, v in value.items():
                if k == "rules" and isinstance(v, list):
                    perms[k] = [r for r in v if isinstance(r, str) and _TIGHTENING_RULE.match(r)]
                    risky += [f"permissions.rules: {r!r}" for r in v if r not in perms[k]]
                elif k in _SAFE_PERMISSIONS and v not in _SAFE_PERMISSIONS[k]:
                    risky.append(f"permissions.{k} = {v!r}")
                else:
                    perms[k] = v
            safe[key] = perms
        elif key in _SAFE_KEYS or key not in known:
            safe[key] = value
        else:
            risky += _describe(key, value)
    # 转义控制字符：防止配置里的终端转义序列把确认提示改写成别的样子
    return safe, ["".join(c if c.isprintable() else repr(c)[1:-1] for c in s) for s in risky]


def _describe(key: str, value: Any) -> list[str]:
    """高危设置写成一行：会执行的命令完整展示（不截断，截断处正好可以藏东西），其余展示取值。"""
    if key == "hooks" and isinstance(value, list) and value:
        return [f"hooks: {h.get('event', 'pre_tool_use')}（{h.get('matcher', '*')}）执行 {h.get('command')!r}"
                if isinstance(h, dict) else f"hooks: {h!r}" for h in value]
    if key == "mcp_servers" and isinstance(value, dict) and value:
        return [f"mcp_servers.{name}: 启动 {' '.join(map(str, [s.get('command'), *s.get('args', [])]))!r}"
                if isinstance(s, dict) else f"mcp_servers.{name} = {s!r}" for name, s in value.items()]
    return [f"{key} = {value!r}"]


def _trusted_part(path: Path, raw: bytes, data: dict[str, Any],
                  trust: Callable[[TrustRequest], bool] | None) -> dict[str, Any]:
    """项目配置：高危设置在这份内容被信任过、或 ``trust`` 回调当场确认后才生效，否则只返回安全的部分。"""
    safe, risky = split_untrusted(data)
    if not risky:
        return data
    req = TrustRequest(path, hashlib.sha256(raw).hexdigest(), risky)
    if is_trusted(req):
        return data
    if trust is None:
        warnings.warn(f"{path} 中的以下设置需要信任才能生效，已忽略：{'；'.join(risky)}。"
                      "确认可信后用 HarnessConfig.load(path=...) 显式加载该文件", UntrustedConfigWarning, stacklevel=3)
    elif trust(req):
        record_trust(req)
        return data
    return safe


def _trust_file() -> Path:
    return flywhl_home() / "trusted.json"


def _trust_records() -> dict[str, Any]:
    try:
        data = json.loads(_trust_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}           # 记录缺失或损坏，按“都没信任过”处理：多问一次，而不是放行
    return data if isinstance(data, dict) else {}


def is_trusted(req: TrustRequest) -> bool:
    rec = _trust_records().get(str(req.path))
    return isinstance(rec, dict) and rec.get("sha256") == req.digest


def record_trust(req: TrustRequest) -> None:
    """记下“这个位置的这份内容可信”。同一路径只保留最近一次确认的内容。"""
    records = _trust_records()
    records[str(req.path)] = {"sha256": req.digest, "trusted_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    f = _trust_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f".{f.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, f)      # 原子替换：并发写入最坏丢一条记录，不会留下半个文件


def _merge(dc: Any, data: dict[str, Any], *, layer: bool = False) -> None:
    """把字典递归合并进 dataclass；未知键直接报错，避免配置拼写错误被静默忽略。

    ``layer=True`` 用于叠加配置文件：``hooks`` 与 ``rules`` 追加到已有列表之后，其余字段照常覆盖。
    """
    names = {f.name: f for f in fields(dc)}
    for key, value in data.items():
        if key not in names:
            raise ValueError(f"未知配置项：{key}（可选：{', '.join(sorted(names))}）")
        current = getattr(dc, key)
        if is_dataclass(current) and isinstance(value, dict):
            _merge(current, value, layer=layer)
        elif key == "hooks" and isinstance(value, list):
            hooks = [h if isinstance(h, HookConfig) else HookConfig(**h) for h in value]
            setattr(dc, key, current + hooks if layer else hooks)
        elif key == "rules" and layer and isinstance(value, list):
            setattr(dc, key, current + value)
        else:
            setattr(dc, key, value)
