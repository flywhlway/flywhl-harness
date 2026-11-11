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
from typing import Any, Callable, Iterable


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
    # auto（探测，用不了时拒绝执行）| off | macos（sandbox-exec）| linux（bubblewrap）：bash、命令 hook、MCP 服务器、验收命令的系统沙箱。
    # off 只在用户配置、-c 指定的配置或命令行里有效，项目配置写的不算。见 tools/confine.py
    sandbox: str = "auto"
    # auto（有沙箱时 allowlist，关掉沙箱时 allow）| allow | deny | allowlist（只经出站代理连 network_allow 里的主机）；后两种要求开启沙箱
    sandbox_network: str = "auto"
    network_allow: list[str] = field(default_factory=list)   # 出站白名单：pypi.org、*.github.com、registry.local:5000
    unix_sockets: list[str] = field(default_factory=list)    # 沙箱里放行连接的 Unix 套接字（绝对路径），如本机数据库；默认只放行 DNS 与系统日志
    # hook / MCP 服务器经 npx、uvx、docker run 这类命令临时拉取的包必须锁到确切版本（镜像要带 @sha256: 摘要），否则拒绝启动
    require_pinned: bool = False
    # 无人值守（on_ask = "deny"）时自动批准的操作，写成 工具(模式)：bash(npm test*)、write(src/**)。其余需要审批的照旧拒绝；
    # 高危命令、托管基线要求确认的、读过不可信内容后的外部操作不在此列，见 PermissionPolicy.auto_approval
    auto_approve: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        from .policy.permissions import auto_approve_rule   # 延迟导入，与 _describe 同理

        _check_str_list("permissions.auto_approve", self.auto_approve)
        for r in self.auto_approve:
            auto_approve_rule(r)    # 写错了在加载时就报出来，不要等到第一次审批


WEB_LEVELS = ("off", "search", "fetch")         # 会话联网能力：fetch 包含 search
FETCH_SCOPES = ("cited", "ask", "public")       # 由严到松
WEB_BACKENDS = ("deepseek", "bocha", "searxng")
FETCH_BACKENDS = ("deepseek", "direct")
# 只能调低的数值项：项目配置写得比用户配置大就丢弃
_WEB_LOWER_ONLY = ("max_searches_per_run", "max_fetches_per_run", "max_results", "result_chars", "fetch_chars",
                   "fetch_max_bytes", "timeout", "cache_ttl")
WEB_BACKEND_KEYS = ("api_key_env", "price_per_call", "send_question", "min_rerank_score", "endpoint", "allow_private")


def web_rank(level: str) -> int:
    return WEB_LEVELS.index(level) if level in WEB_LEVELS else 0


# 打开供应商侧联网的 extra_body 键（百炼、OpenAI 兼容的 web_search_options 等）。搜索发生在模型供应商那里，
# 结果直接进模型上下文：harness 看不到、不计数、不审计。识别出来只为告警和置污点，不阻止
NATIVE_SEARCH_KEYS = ("enable_search", "search_options", "forced_search", "web_search_options", "enable_source",
                      "enable_citation")


def native_web_search(extra_body: dict[str, Any] | None) -> list[str]:
    """``extra_body`` 里打开模型侧联网的字段名（只看值为真的；``tools`` 里 type 以 web_search 开头的也算）。"""
    body = extra_body or {}
    hits = [k for k in NATIVE_SEARCH_KEYS if body.get(k) not in (None, False, 0, "", {}, [])]
    tools = body.get("tools")
    if isinstance(tools, list) and any(isinstance(t, dict) and str(t.get("type", "")).startswith("web_search") for t in tools):
        hits.append("tools[].type=web_search")
    return hits


@dataclass
class WebConfig:
    """内置联网工具 web_search / web_fetch。默认关闭；开启后每个会话在开工前选能力（off / search / fetch），开工后只能收紧。

    后端按 ``backends`` 的顺序使用，鉴权、余额、服务故障时换下一家。密钥只写环境变量名（``[web.backend.<名字>]``
    的 ``api_key_env``），不进 /api 接口、事件和日志。项目配置里的 ``[web]`` 只能收紧，见 ``_keep_web_tight``。
    """

    enabled: bool = False
    default: str = "off"                       # 新会话默认能力
    backends: list[str] = field(default_factory=lambda: ["deepseek"])
    fetch_backend: str = "deepseek"            # deepseek（服务端抓取）| direct（本机直连）
    max_searches_per_run: int = 20
    max_fetches_per_run: int = 10
    max_results: int = 8                       # 单次搜索返回条数上限
    result_chars: int = 6000                   # 单次搜索交给模型的总字符上限
    fetch_chars: int = 20000                   # 单页正文交给模型的字符上限
    fetch_max_bytes: int = 2_000_000           # 响应体（解压后）字节上限
    fetch_scope: str = "ask"                   # cited | ask | public
    fetch_allow: list[str] = field(default_factory=list)    # 额外自动放行读取的主机，写法同 permissions.network_allow
    deny_domains: list[str] = field(default_factory=list)   # 搜索结果过滤 + 禁止读取，含子域名
    timeout: float = 15.0
    cache_ttl: float = 600.0                   # 同会话同参数的搜索结果缓存秒数，0 关闭
    backend: dict[str, dict[str, Any]] = field(default_factory=dict)   # [web.backend.<名字>]
    # 改用模型供应商自带的搜索（llm/native.py 里已确认的那几家）：会话开“搜索”时不注册 web_search，
    # 搜索在供应商那边发生；主模型不在声明里时照常用内置工具
    native: bool = False

    def __post_init__(self) -> None:
        check_web(self)


def check_web(w: "WebConfig") -> "WebConfig":
    """``[web]`` 写错在加载时就报错。_merge 用 setattr 赋值，不经过 __post_init__，所以加载完再调一次。"""
    from .tools.egress import parse_rules   # 延迟导入：tools 依赖本模块

    for k in ("enabled", "native"):
        if not isinstance(getattr(w, k), bool):
            raise ValueError(f"web.{k} 必须是 true 或 false，实际是 {getattr(w, k)!r}")
    if w.default not in WEB_LEVELS:
        raise ValueError(f"web.default 只能是 {'、'.join(WEB_LEVELS)}，实际是 {w.default!r}")
    if w.fetch_scope not in FETCH_SCOPES:
        raise ValueError(f"web.fetch_scope 只能是 {'、'.join(FETCH_SCOPES)}，实际是 {w.fetch_scope!r}")
    if w.fetch_backend not in FETCH_BACKENDS:
        raise ValueError(f"web.fetch_backend 只能是 {'、'.join(FETCH_BACKENDS)}，实际是 {w.fetch_backend!r}")
    for b in _check_str_list("web.backends", w.backends):
        if b not in WEB_BACKENDS:
            raise ValueError(f"web.backends 里的 {b!r} 不认识（可选：{'、'.join(WEB_BACKENDS)}）")
    for k in _WEB_LOWER_ONLY:
        v = getattr(w, k)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 or (v == 0 and k not in ("cache_ttl",)):
            raise ValueError(f"web.{k} 必须是正数，实际是 {v!r}")
    parse_rules(_check_str_list("web.fetch_allow", w.fetch_allow))
    for d in _check_str_list("web.deny_domains", w.deny_domains):
        if not d.strip() or "/" in d:
            raise ValueError(f"web.deny_domains 写主机名或域名（如 example.com），实际是 {d!r}")
    if not isinstance(w.backend, dict):
        raise ValueError(f"web.backend 必须是表，实际是 {w.backend!r}")
    for name, spec in w.backend.items():
        if name not in WEB_BACKENDS:
            raise ValueError(f"[web.backend.{name}] 不认识（可选：{'、'.join(WEB_BACKENDS)}）")
        if not isinstance(spec, dict):
            raise ValueError(f"[web.backend.{name}] 必须是表")
        unknown = sorted(set(spec) - set(WEB_BACKEND_KEYS))
        if unknown:
            raise ValueError(f"[web.backend.{name}] 有未知配置项：{', '.join(unknown)}（可选：{', '.join(WEB_BACKEND_KEYS)}）")
        price = spec.get("price_per_call", 0)
        if isinstance(price, bool) or not isinstance(price, (int, float)) or price < 0:
            raise ValueError(f"[web.backend.{name}].price_per_call 必须是非负数，实际是 {price!r}")
        for k in ("send_question", "allow_private"):
            if not isinstance(spec.get(k, False), bool):
                raise ValueError(f"[web.backend.{name}].{k} 必须是 true 或 false")
        for k in ("api_key_env", "endpoint"):
            if k in spec and not isinstance(spec[k], str):
                raise ValueError(f"[web.backend.{name}].{k} 必须是字符串")
    return w


DURABILITY_LEVELS = ("full", "normal")


def check_durability(value: Any) -> str:
    if value not in DURABILITY_LEVELS:
        raise ValueError(f"durability 只能是 {'、'.join(DURABILITY_LEVELS)}，实际是 {value!r}")
    return value


@dataclass
class HookConfig:
    """进程外命令 hook（协议与 Claude Code 兼容：stdin JSON，exit 2 阻断）。"""

    event: str = "pre_tool_use"
    command: str = ""
    matcher: str = "*"
    timeout: float = 30.0
    # 脚本崩溃、超时、输出无法解析时怎么办。None = 按事件决定：pre_tool_use 是守卫，失败即拒绝（fail-closed）；
    # 其它事件只是观察或补充上下文，失败即放行并给出警告
    fail_closed: bool | None = None
    # 子进程与 bash、MCP 一样剥离密钥类环境变量。确实要用的：env 写明取值，pass_env 列出从 harness 进程原样传入的变量名
    env: dict[str, str] = field(default_factory=dict)
    pass_env: list[str] = field(default_factory=list)
    # 这个 hook 单独的沙箱设置，没写的跟随 permissions。sandbox = "off" 只在用户配置里有效，含义与 [mcp_servers.<名字>] 相同
    sandbox: str | None = None
    sandbox_network: str | None = None
    network_allow: list[str] | None = None

    def __post_init__(self) -> None:
        _check_env("hooks.env", self.env)
        _check_str_list("hooks.pass_env", self.pass_env)
        _check_entry_sandbox("hooks", self.__dict__)

    @property
    def closed(self) -> bool:
        return self.fail_closed if self.fail_closed is not None else self.event == "pre_tool_use"


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
    trust_workspace: bool = False          # 信任所有工作区。项目配置写的丢弃，只在用户配置、-c 指定的文件里有效，见 is_workspace_trusted
    # ---- 运行时 ----
    workspace: str = "."
    data_dir: str = ".flywhl"
    tool_timeout: float = 120.0
    parallel_tool_limit: int = 8
    cancel_grace_seconds: float = 5.0      # 不可中断工具在取消后的宽限期
    heartbeat_seconds: float = 2.0
    stale_after_seconds: float = 10.0      # 心跳超过该时长视为 Worker 崩溃
    snapshots: bool = True                 # 写操作前用影子 git 打工作区快照
    # 会话库的落盘强度：full 每次提交都刷到盘上，断电不丢已提交的事务；normal 提交更快，断电可能丢最后几次提交，
    # 其中可能有执行前写的工具 intent，恢复时看不到记录，非幂等调用会再执行一次。影子 git 和工作区文件都不在这个保证里
    durability: str = "full"
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
    web: WebConfig = field(default_factory=WebConfig)

    def __post_init__(self) -> None:
        check_durability(self.durability)

    # ------------------------------------------------------------------ 加载
    @classmethod
    def load(cls, path: str | Path | None = None, *, trust: Callable[[TrustRequest], bool] | None = None,
             **overrides: Any) -> "HarnessConfig":
        """加载顺序（后者覆盖前者）：默认值 → 用户配置 → 项目配置 → 环境变量 → 显式参数。

        - 用户配置 ``~/.flywhl/config.toml``（目录见 ``flywhl_home()``）与 ``path`` 显式指定的文件视为可信；
        - 不给 ``path`` 时从启动目录找 ``flywhl.toml`` / ``.flywhl/config.toml``。其中的高危设置只在这份文件的
          当前内容被信任过之后生效；未信任时交给 ``trust`` 回调决定（返回 True 即记入 ``trusted.json``），
          没有回调则忽略这些设置并发出 ``UntrustedConfigWarning``；
        - 两层文件里的 ``hooks`` 与 ``permissions.rules`` 是追加而不是替换：项目配置删不掉用户配置里的守卫 hook 与 deny 规则；
        - 显式参数只给了 ``provider``（不是 auto）时，前几层的模型若自带别家供应商（``deepseek:…``、只写供应商名、
          演示模型名）就丢掉，改用该供应商的默认模型；不带供应商的模型名照常保留。
        """
        cfg = cls()
        user_file = (flywhl_home() / "config.toml").resolve()
        if user_file.is_file():
            _merge(cfg, tomllib.loads(user_file.read_text(encoding="utf-8")), layer=True)
        explicit = {k: v for k, v in overrides.items() if v is not None}
        project = Path(path) if path else next((p for p in map(Path, PROJECT_CONFIG_FILES) if p.is_file()), None)
        if project is not None and project.is_file() and project.resolve() != user_file:
            raw = project.read_bytes()      # 只读一次：哈希与解析的必须是同一份内容
            data = tomllib.loads(raw.decode("utf-8"))
            if not path:
                data = _no_self_trust(_only_turn_off(_keep_sandbox_on(data, project.resolve())), project.resolve())
                data = _keep_web_tight(data, cfg.web, project.resolve())
                data = _trusted_part(project.resolve(), raw, data, trust, _run_dir(cfg, data, explicit),
                                     cfg.permissions.auto_approve)
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
        if explicit.get("provider", "auto") != "auto" and "model" not in explicit:
            from .llm.profiles import pinned_provider   # 延迟导入：llm 包反过来依赖本模块

            # resolve 让模型自带的供应商优先于 provider：下层留下的 deepseek:… 会把显式的 provider=mock 改走
            if pinned_provider(cfg.model) not in (None, explicit["provider"]):
                cfg.model = None
        _merge(cfg, explicit)
        check_web(cfg.web)
        return cfg

    def data_path(self) -> Path:
        p = Path(self.data_dir)
        if not p.is_absolute():
            p = Path(self.workspace).resolve() / p
        return ensure_data_dir(p)


# 数据目录默认全部忽略，以后新增的文件也一样。只放行用户自己的配置和技能，它们可能要提交
DATA_DIR_IGNORE = "*\n!config.toml\n!skills/\n!skills/**\n"


def ensure_data_dir(path: Path) -> Path:
    """建数据目录。新建时只给属主：里面有会话库、审批记录和快照，它们也不能被 ``git add -A`` 带进仓库。
    目录可能早就有了（比如先写了 config.toml），所以不看是谁建的，还没有 .gitignore 就写一份，已有的不动。"""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():    # 目录在别处，不归工作区的 git 管；克隆来的仓库也可能拿它指向工作区外
        return path
    try:   # O_EXCL 不跟随符号链接：.flywhl/.gitignore 本身也可能是悬空链接
        fd = os.open(path / ".gitignore", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except OSError:     # 已经有了，或者写不了，都不影响运行
        return path
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(DATA_DIR_IGNORE)
    return path


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
# 决定请求发往哪里、记在谁的账上；workspace / data_dir 决定 Agent 在哪里读写；
# system_prompt / append_system_prompt 是 harness 对模型说的话，不能由仓库作者来写。新增字段默认落在需要信任的一侧。
_SAFE_KEYS = frozenset({
    "provider", "model", "fallback_models", "max_tokens", "temperature", "thinking", "reasoning_effort",
    "request_timeout", "max_retries", "load_agents_md", "load_skills",
    "tool_timeout", "parallel_tool_limit", "cancel_grace_seconds", "heartbeat_seconds", "stale_after_seconds",
    "snapshots", "max_stop_blocks", "verify_todos", "subagent_max_depth", "enable_demo_tools",
    "context", "budget", "loop_guard",
    "web",   # 先经过 _keep_web_tight：到这里只剩收紧的部分
})
# permissions 按取值判断：收紧审批的取值直接生效，放宽的（bypass、accept_edits、on_ask=allow、不剥离密钥）要信任。
# on_ask = deny 有一个例外：用户配置里写了 auto_approve 规则时，它会让这些规则替人批准，见 split_untrusted
_SAFE_PERMISSIONS: dict[str, tuple[Any, ...]] = {
    "mode": ("default", "plan"), "on_ask": ("wait", "deny"), "strip_secret_env": (True,), "sandbox": ("auto", "macos", "linux"),
    # allowlist 比 allow 严、比 deny 松，不知道用户配置里是哪一个，所以和白名单条目一样要信任
    "sandbox_network": ("deny",), "network_allow": ([],),
    "unix_sockets": ([],),   # 放行的套接字可能连着 docker.sock 这类能绕开沙箱的守护进程
    "require_pinned": (True,),
    "auto_approve": ([],),   # 无人值守时多批准哪些操作，由仓库自己说了算就等于没有审批
}
_TIGHTENING_RULE = re.compile(r"^\s*(deny|ask)\s*:")


class UntrustedConfigWarning(UserWarning):
    """项目配置里的高危设置因为没有被信任而被忽略。"""


@dataclass
class TrustRequest:
    """一份等待信任的项目配置，交给 ``HarnessConfig.load(trust=...)`` 的回调判断。"""

    path: Path          # 绝对路径。信任绑定位置：同样一行 python scripts/guard.py，换个仓库执行的就是另一份脚本
    digest: str         # 文件内容（连同 hook、MCP 引用的脚本）的 SHA-256，哪个变了都要重新确认
    risky: list[str]    # 需要信任才能生效的设置，每项一行，供人审阅


def flywhl_home() -> Path:
    """用户级目录：用户配置 config.toml 与信任记录 trusted.json 所在处。``FLYWHL_HOME`` 可改（测试、容器）。"""
    return Path(os.environ.get("FLYWHL_HOME") or "~/.flywhl").expanduser()


# 项目配置写了也丢弃的取值，信任过也一样：关沙箱、放开网络都是用户的决定，仓库只能收紧。具体主机的 network_allow 不在其中，走信任确认
_NEVER_FROM_PROJECT = {"sandbox": "off", "sandbox_network": "allow"}


def _keep_sandbox_on(data: dict[str, Any], path: Path) -> dict[str, Any]:
    """项目配置关不掉沙箱，也放不开沙箱的网络：``permissions`` 与 hook、MCP 服务器各自的设置都去掉。
    放开网络和关沙箱同等处理，否则工作区靠一次信任确认就能被传走。用户配置、``-c`` 指定的文件、命令行不经过这里。"""
    dropped: list[str] = []

    def keep(spec: Any, where: str) -> Any:
        if not isinstance(spec, dict):
            return spec
        bad = [k for k, v in _NEVER_FROM_PROJECT.items() if spec.get(k) == v]
        dropped.extend(f"{where}.{k} = {spec[k]!r}" for k in bad)
        return {k: v for k, v in spec.items() if k not in bad}

    out = dict(data)
    if "permissions" in data:
        out["permissions"] = keep(data["permissions"], "permissions")
    if isinstance(data.get("hooks"), list):
        out["hooks"] = [keep(h, f"hooks[{i}]") for i, h in enumerate(data["hooks"])]
    if isinstance(data.get("mcp_servers"), dict):
        out["mcp_servers"] = {n: keep(s, f"mcp_servers.{n}") for n, s in data["mcp_servers"].items()}
    if dropped:
        warnings.warn(f"{path} 里的 {'、'.join(dropped)} 不生效：项目配置不能关闭系统沙箱，也不能放开它的网络。确实要改，写在用户配置 "
                      f"{flywhl_home() / 'config.toml'} 里，或者启动时加 --sandbox off、--network allow",
                      UntrustedConfigWarning, stacklevel=3)
    return out


def _keep_web_tight(data: dict[str, Any], user: WebConfig, path: Path) -> dict[str, Any]:
    """项目配置里的 ``[web]`` 只能收紧，信任过也一样：联网把工作区的内容送到第三方，开不开、送给谁是用户的决定。

    - ``enabled``、``native`` 只能写 false；``default``、``fetch_scope`` 只能比用户配置更严；数值上限只能调低；
    - ``backends``、``fetch_backend``、``[web.backend.*]``、``fetch_allow`` 一律丢弃：它们决定查询词和网址发给谁、
      从哪台机器发出（``fetch_backend = "direct"`` 把抓取搬到本机）；
    - ``deny_domains`` 与用户配置合并，只增不减。"""
    spec = data.get("web")
    if spec is None:
        return data
    if not isinstance(spec, dict):
        return data   # 交给 _merge 报类型错误
    dropped: list[str] = []
    out: dict[str, Any] = {}
    for k, v in spec.items():
        if k in ("enabled", "native"):
            ok = v is False   # native = true 会把查询词交给模型供应商：只认用户配置
        elif k == "default":
            ok = v in WEB_LEVELS and web_rank(v) <= web_rank(user.default)
        elif k == "fetch_scope":
            ok = v in FETCH_SCOPES and FETCH_SCOPES.index(v) <= FETCH_SCOPES.index(user.fetch_scope)
        elif k in _WEB_LOWER_ONLY:
            ok = isinstance(v, (int, float)) and not isinstance(v, bool) and v <= getattr(user, k)
        elif k == "deny_domains":
            ok = isinstance(v, list)
            if ok:
                v = list(dict.fromkeys([*user.deny_domains, *v]))
        elif k in ("backends", "fetch_backend", "backend", "fetch_allow"):
            ok = False
        else:
            ok = True   # 未知键交给 _merge 报错
        if ok:
            out[k] = v
        else:
            dropped.append(f"web.{k} = {v!r}")
    if dropped:
        warnings.warn(f"{path} 里的 {'、'.join(dropped)} 不生效：项目配置里的 [web] 只能收紧（关闭联网、调低上限、收窄读取范围、"
                      f"增加禁止的域名），后端与密钥只认用户配置 {flywhl_home() / 'config.toml'} 或 -c 指定的文件",
                      UntrustedConfigWarning, stacklevel=3)
    return {**data, "web": out}


def _no_self_trust(data: dict[str, Any], path: Path) -> dict[str, Any]:
    """仓库不能宣布自己可信，配置被信任过也一样：信任配置是同意它的 hook 和模型设置，不是同意把它的文件当成自己人写的。"""
    if "trust_workspace" not in data:
        return data
    if data["trust_workspace"]:
        warnings.warn(f"{path} 里的 trust_workspace 不生效：项目配置不能信任自己所在的工作区。确实要信任，启动时加 --trust，"
                      f"或者写在用户配置 {flywhl_home() / 'config.toml'} 里", UntrustedConfigWarning, stacklevel=3)
    return {k: v for k, v in data.items() if k != "trust_workspace"}


def _only_turn_off(data: dict[str, Any]) -> dict[str, Any]:
    """项目文件的加载，项目配置只能关不能开：用户为了看陌生仓库关掉的，仓库不能自己打开。"""
    return {k: v for k, v in data.items() if k not in ("load_agents_md", "load_skills") or v is False}


def split_untrusted(data: dict[str, Any], base: Path | None = None,
                    auto_approve: Iterable[str] = ()) -> tuple[dict[str, Any], list[str]]:
    """把来自不可信位置的配置拆成 (可以直接生效的部分, 需要信任的设置)。base 是 hook、MCP 服务器实际运行的目录
    （工作区）：命令里的相对路径按它解析，引用的文件与没锁版本的包写进确认提示。

    auto_approve 是前几层（用户配置）已有的自动批准规则。没有它们时 ``on_ask = "deny"`` 只会更严；有了它们，
    deny 就是把“等人审”换成“按这些规则替人批准”，仓库不能替用户打开，同样要信任。

    未知键留在前一部分，交给 ``_merge`` 照常报错——拼写错误不该变成一次信任确认。
    """
    known = {f.name for f in fields(HarnessConfig)}
    safe: dict[str, Any] = {}
    risky: list[str] = []
    for key, value in data.items():
        if key == "durability":     # 取值写错和键写错一样直接报错，不该变成一次信任确认，也不该被当成未信任悄悄忽略
            check_durability(value)
        if key == "permissions" and isinstance(value, dict):
            perms: dict[str, Any] = {}
            for k, v in value.items():
                if k == "rules" and isinstance(v, list):
                    perms[k] = [r for r in v if isinstance(r, str) and _TIGHTENING_RULE.match(r)]
                    risky += [f"permissions.rules: {r!r}" for r in v if r not in perms[k]]
                elif k in _SAFE_PERMISSIONS and v not in _SAFE_PERMISSIONS[k]:
                    risky.append(f"permissions.{k} = {v!r}")
                elif k == "on_ask" and v == "deny" and value.get("auto_approve", list(auto_approve)):
                    risky.append("permissions.on_ask = 'deny'（会按 auto_approve 规则自动批准，不再等人审）")
                else:
                    perms[k] = v
            safe[key] = perms
        elif key in _SAFE_KEYS or key not in known:
            safe[key] = value
        else:
            risky += _describe(key, value, base or Path.cwd())
    # 转义控制字符：防止配置里的终端转义序列把确认提示改写成别的样子
    return safe, ["".join(c if c.isprintable() else repr(c)[1:-1] for c in s) for s in risky]


def _describe(key: str, value: Any, base: Path) -> list[str]:
    """高危设置写成一行：会执行的命令完整展示（不截断，截断处正好可以藏东西），其余展示取值。"""
    from .policy.supply_chain import describe

    if key == "hooks" and isinstance(value, list) and value:
        return [f"hooks: {h.get('event', 'pre_tool_use')}（{h.get('matcher', '*')}）执行 {h.get('command')!r}{_describe_env(h)}"
                + (f"；沙箱网络策略 {h['sandbox_network']!r}" if h.get("sandbox_network") else "")
                + (f"；出站白名单 {h['network_allow']!r}" if h.get("network_allow") else "")
                + (describe(h["command"], base, h.get("env")) if isinstance(h.get("command"), str) else "")
                if isinstance(h, dict) else f"hooks: {h!r}" for h in value]
    if key == "mcp_servers" and isinstance(value, dict) and value:
        return [_describe_mcp(name, s, base) for name, s in value.items()]
    return [f"{key} = {value!r}"]


def _describe_env(spec: dict[str, Any]) -> str:
    """子进程的 env / pass_env 同样要经过确认：PATH、BASH_ENV、DYLD_* 能换掉命令实际执行的程序，pass_env 会把密钥交出去。"""
    from .policy.bash_analyzer import BENIGN_ENV   # 延迟导入，与 load() 里的 llm.profiles 同理
    from .tools.shell import _SECRET_ENV

    env = spec.get("env")
    parts = [f"设置 {k}=" + ("<已隐藏>" if _SECRET_ENV.search(str(k)) else repr(v))
             + ("" if BENIGN_ENV.fullmatch(str(k)) else "（可能改变实际执行的程序或加载的代码）")
             for k, v in (env.items() if isinstance(env, dict) else [])]
    if spec.get("pass_env"):
        parts.append(f"从当前环境原样传入 {spec['pass_env']!r}")
    return "".join(f"；{p}" for p in parts)


def _describe_mcp(name: str, spec: Any, base: Path) -> str:
    from .policy.supply_chain import describe, mcp_argv, mcp_base

    try:
        check_mcp_server(name, spec)
    except ValueError as e:
        return f"{e}（信任后加载时同样会报错）"
    line = f"mcp_servers.{name}: 启动 {' '.join(map(str, [spec.get('command'), *spec.get('args', [])]))!r}"
    if spec.get("cwd"):
        line += f"（目录 {spec['cwd']!r}）"
    line += _describe_env(spec)
    if spec.get("sandbox_network"):
        line += f"；沙箱网络策略 {spec['sandbox_network']!r}"
    if spec.get("network_allow"):
        line += f"；出站白名单 {spec['network_allow']!r}"
    if spec.get("trust_annotations"):
        line += "；采信服务器自报的只读、幂等、封闭世界注解"
    if spec.get("read_only_tools"):
        line += f"；这些工具按只读处理，调用不经审批：{spec['read_only_tools']!r}"
    return line + describe(mcp_argv(spec), mcp_base(spec, base), spec.get("env"))


def _run_dir(cfg: HarnessConfig, data: dict[str, Any], explicit: dict[str, Any]) -> Path:
    """hook 与 MCP 服务器实际运行的目录，即最终生效的工作区：显式参数 > 环境变量 > 项目配置 > 用户配置。
    项目配置里的 workspace 本身也要信任才生效；不信任时连同 hook 一起丢掉，按它算也不会错。"""
    ws = explicit.get("workspace") or os.environ.get("FLYWHL_WORKSPACE") or data.get("workspace") or cfg.workspace
    return Path(str(ws)).expanduser().resolve()


def _trusted_part(path: Path, raw: bytes, data: dict[str, Any],
                  trust: Callable[[TrustRequest], bool] | None, base: Path,
                  auto_approve: Iterable[str] = ()) -> dict[str, Any]:
    """项目配置：高危设置在这份内容被信任过、或 ``trust`` 回调当场确认后才生效，否则只返回安全的部分。

    hook、MCP 命令里引用的脚本（``python3 scripts/guard.py``）真正执行的是脚本的内容：一并算进哈希，改了脚本同样要
    重新确认。相对路径按 base（hook、MCP 实际运行的工作区）解析，不按配置文件所在目录：换个工作区启动，执行的是
    另一份脚本，哈希跟着变，要重新确认。"""
    from .policy.supply_chain import config_files, file_digest

    safe, risky = split_untrusted(data, base, auto_approve)
    if not risky:
        return data
    digest = hashlib.sha256(raw)
    for f in config_files(data, base):
        digest.update(f"\0{f}\0{file_digest(f)}".encode())
    req = TrustRequest(path, digest.hexdigest(), risky)
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
    _save_trust(records)


def trust_root(path: str | Path) -> Path:
    """工作区信任的单位：所在的 git 仓库根，不在仓库里时是这个目录本身。子目录跟着仓库走；vendor/ 里的嵌套仓库是另一个作者写的，
    单独算。解析到真实路径：软链接换个名字还是同一个工作区。"""
    cur = Path(path).expanduser().resolve()
    return next((d for d in [cur, *cur.parents] if (d / ".git").exists()), cur)


def is_workspace_trusted(path: str | Path) -> bool:
    """用户说过这个工作区里的文件是自己人写的吗。没说过的工作区，文件内容算不可信（污点来源），项目指令文件标注为不可信。
    只认 ``trusted.json`` 里用户亲自确认的记录：这个文件在工作区之外，沙箱里写不到，文件工具对 ``.flywhl/`` 一律拒绝。
    记录绑定路径，不绑定内容：工作区每天都在变。删掉仓库再在同一路径克隆一个别的，信任还在。"""
    rec = _workspace_records().get(str(trust_root(path)))
    return isinstance(rec, dict)


def record_workspace_trust(path: str | Path) -> None:
    records = _trust_records()
    records["workspaces"] = {**_workspace_records(records), str(trust_root(path)): {"trusted_at": time.strftime("%Y-%m-%d %H:%M:%S")}}
    _save_trust(records)


def _workspace_records(records: dict[str, Any] | None = None) -> dict[str, Any]:
    found = (_trust_records() if records is None else records).get("workspaces")
    return found if isinstance(found, dict) else {}


def _save_trust(records: dict[str, Any]) -> None:
    f = _trust_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f".{f.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, f)      # 原子替换：并发写入最坏丢一条记录，不会留下半个文件


# ---------------------------------------------------------------------- 类型校验
# 放宽权限的设置一律按声明的类型严格校验，写错就报错，不做宽松转换：read_only_tools = "safe_*" 若按字符拆开，
# 其中的 "*" 会放行全部工具，而信任确认里显示的却是 'safe_*'——确认的内容必须就是生效的内容
MCP_SERVER_KEYS = ("command", "args", "env", "cwd", "read_only_tools", "trust_annotations", "sandbox", "sandbox_network",
                   "network_allow")


def _check_str_list(where: str, value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ValueError(f"{where} 必须是字符串列表，实际是 {value!r}")
    return value


def _check_env(where: str, value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise ValueError(f"{where} 必须是“变量名 = 字符串”的表，实际是 {value!r}")
    return value


def _check_entry_sandbox(where: str, spec: dict[str, Any]) -> None:
    """hook、MCP 服务器单独的沙箱设置。``sandbox`` 只有 off 这一个取值：其余档位跟随全局，两边不一致时说不清谁生效。"""
    from .tools.egress import parse_rules   # 延迟导入：tools 依赖本模块

    if spec.get("sandbox") not in (None, "off"):
        raise ValueError(f"{where}.sandbox 只能写 \"off\"，其余档位跟随 permissions.sandbox，实际是 {spec['sandbox']!r}")
    if spec.get("sandbox_network") not in (None, "allow", "deny", "allowlist"):
        raise ValueError(f"{where}.sandbox_network 只能是 allow、deny、allowlist，实际是 {spec['sandbox_network']!r}")
    if spec.get("network_allow") is not None:
        parse_rules(_check_str_list(f"{where}.network_allow", spec["network_allow"]))


def check_mcp_server(name: str, spec: Any) -> dict[str, Any]:
    """校验一个 ``[mcp_servers.<name>]``，有误抛 ``ValueError``。配置加载与 ``load_mcp_tools`` 都会调用。"""
    where = f"mcp_servers.{name}"
    if not isinstance(spec, dict):
        raise ValueError(f"{where} 必须是表，实际是 {spec!r}")
    unknown = sorted(set(spec) - set(MCP_SERVER_KEYS))
    if unknown:
        raise ValueError(f"{where} 有未知配置项：{', '.join(unknown)}（可选：{', '.join(MCP_SERVER_KEYS)}）")
    if not isinstance(spec.get("command"), str) or not spec["command"]:
        raise ValueError(f"{where}.command 必须是非空字符串")
    _check_str_list(f"{where}.args", spec.get("args", []))
    _check_env(f"{where}.env", spec.get("env", {}))
    if not isinstance(spec.get("cwd", ""), str):
        raise ValueError(f"{where}.cwd 必须是字符串")
    _check_str_list(f"{where}.read_only_tools", spec.get("read_only_tools", []))
    if not isinstance(spec.get("trust_annotations", False), bool):
        raise ValueError(f"{where}.trust_annotations 必须是 true 或 false，实际是 {spec['trust_annotations']!r}")
    _check_entry_sandbox(where, spec)
    return spec


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
        elif key == "mcp_servers":
            if not isinstance(value, dict):
                raise ValueError(f"mcp_servers 必须是表，实际是 {value!r}")
            setattr(dc, key, {name: check_mcp_server(name, spec) for name, spec in value.items()})
        elif key == "durability":   # setattr 不经过 __post_init__
            setattr(dc, key, check_durability(value))
        else:
            setattr(dc, key, value)
