# 真实模型测试

离线测试（`tests/test_*.py`）用脚本化模型验证 harness 的逻辑：结果确定、零成本、能进 CI。但协议细节与模型行为只有真实模型能回答——
DeepSeek 要求回传 `reasoning_content`、智谱把余额不足包装成 429、OpenRouter 拒绝 `fallbacks: "default"`，都是在这里发现的。

```bash
.venv/bin/python -m pytest -m live tests/live                 # 全部场景；默认模型 deepseek:deepseek-flash
.venv/bin/python -m pytest -m live tests/live -k compaction   # 单个场景
FLYWHL_LIVE_MODEL=kimi:kimi-k2.6 .venv/bin/python -m pytest -m live tests/live
```

- 密钥从项目根目录的 `.env` 读取（只在这里加载；普通的 `pytest` 不运行这些用例，也不会读到本机密钥）；
  缺少某家密钥的用例自动跳过，不算失败。
- 测试报告末尾打印每个场景的 token、缓存命中与费用。用 deepseek-flash 跑完全部 33 个场景约 3 分钟、30 万 token、0.05 美元左右
  （其中 64% 的输入命中前缀缓存）。
- 断言只针对 harness 的可观测结果（工具序列、文件内容、Run 状态、事件），提示词要求原样输出时才匹配文字。
  模型偶尔不按要求做时，失败信息里附带完整对话记录（`transcript()`），可以直接判断是 harness 的问题还是模型的问题。

## 需要哪些密钥

每个用例开头用 `need()` 检查自己要的密钥，缺了就 **跳过**（skipped），不会失败。没有任何密钥时 33 个全部跳过——
所以 live 用例的 **失败** 一定是“有密钥但没跑通”，原因只剩网络、供应商侧或 harness 缺陷三类，见下文的分类记录。

| 密钥 | 用例 |
|---|---|
| 被测模型的密钥（默认 `DEEPSEEK_API_KEY`；换 `FLYWHL_LIVE_MODEL` 后是那家的） | 24 个：Agent 机制 12 个（跨供应商续聊除外），接入层 6 个（自检、思考开关、前缀缓存、截断、错误分类、模型不存在降级），运行时全部 6 个 |
| `DEEPSEEK_API_KEY` | DeepSeek 的 Anthropic 协议端点 |
| `DASHSCOPE_API_KEY` | 百炼多步任务；跨供应商降级（另需被测模型的密钥） |
| `KIMI_API_KEY` 或 `MOONSHOT_API_KEY` | Kimi 多步任务 |
| `MINIMAX_API_KEY` | MiniMax 多步任务 |
| `OPENROUTER_API_KEY` | OpenRouter 多步任务；经 OpenRouter 调 Claude Haiku（Anthropic 协议） |
| `DEEPSEEK_API_KEY` + Kimi + `MINIMAX_API_KEY` | 同一会话依次换用三家供应商续聊（共 4 次运行） |
| `LLM_FALLBACK_MODEL`（及链上每一家的密钥） | 项目 `.env` 配置的降级链 |

## 失败分类记录（2026-10-16）

本机 `.pytest_cache` 里留有 2026-10-14 一次运行的 30 个失败（当时存在的用例几乎全部失败，横跨五家供应商），运行日志没有保留。
按“缺密钥 / 网络与环境 / 真实缺陷”逐项排查：

| 分类 | 个数 | 依据 |
|---|---|---|
| 缺密钥 | 0 | 不带 `.env`、清空环境变量运行，33 个全部 skipped；缺密钥不会造成失败 |
| 运行环境 / 网络（推断） | 29 | 同样的密钥在 de488df 上重跑全部通过；原始日志缺失，无法确定是沙箱断网、代理还是供应商侧 |
| 真实缺陷 | 1 | `test_plan_mode_is_read_only`：见下文缺陷 1 |

修复过程中又复跑了 4 次，plan 模式用例在第二次失败时暴露出缺陷 2。另有两次失败属于模型随机性：
一次是压缩用例里模型没有真正读完日志，上下文没涨到阈值；一次是跨供应商续聊时模型读到“第 0 个数字是 10”却回答 13。
两次单独重跑都通过，没有改代码。

**缺陷 1：执行器记录的副作用等级与权限判定口径不一致。** 模型在 plan 模式下用绝对路径 `cat /ws/app.py` 查看文件。
权限层带着工作区分析，判为只读并放行；执行器调用 `effect_for()` 时没传工作区，把同一条命令记成 external。
界面、预写 intent、重试与取消语义因此都按另一个等级处理。修复后 `Tool.effect_for()` 接收工作区根目录，执行器与权限层同一口径。
回归测试是 `tests/test_policy.py::test_recorded_effect_matches_permission_verdict`。

**缺陷 2：plan 模式下规则可以放行写操作。** 规则先于模式评估，命中后就不再看模式。模型看到 `ls` 输出里的 `.flywhl` 后，
常写 `find . -not -path './.flywhl/*' -exec …`。这条命令命中托管基线 `ask:bash(*.flywhl*)` 变成审批，live 配置自动批准，
于是在规划阶段执行了写操作。用户的 allow 规则与会话授权也有同样问题。修复后 plan 模式对非只读操作是硬上限。
回归测试是 `tests/test_policy.py::test_plan_mode_is_a_hard_ceiling_for_side_effects`。

全部修复后完整重跑（f212ce0）：33 passed，34.4 万 token，0.051 美元。

## 场景

| 文件 | 场景 |
|---|---|
| `test_live_provider.py` | 接入自检；思考开关；前缀缓存命中；`max_tokens` 截断；真实错误分类（模型不存在、密钥无效）；降级链（模型不存在 → 可用模型）；跨供应商降级（密钥失效 → 百炼）；项目 `.env` 配置的降级链逐项可用且能接手；百炼 / Kimi / MiniMax / OpenRouter 各跑一次多步任务；Anthropic 协议（经 OpenRouter 的 Claude Haiku 5.5、DeepSeek 的 Anthropic 端点） |
| `test_live_agent.py` | 多步只读任务与思考回传；同一轮并行调用；写入审批与 diff 预览；拒绝理由引导模型换方案；编辑 + Stop 验收命令；计划全部完成才允许结束；规划模式只读；子代理结构化回传契约；上下文压缩后仍记得关键事实；预算耗尽时无工具收尾；文件中的提示注入不被执行；超大输出用句柄按需取回；同一会话跨四家供应商续聊 |
| `test_live_runtime.py` | 流式中途取消后会话仍可继续；运行中插话被采纳；安全点暂停与恢复；`kill -9` 后另一个进程接管并完成；真实轨迹的确定性回放；Web API 端到端（REST 建会话 → SSE 事件 → 审批接口批准） |

## 录制的夹具

`tests/fixtures/sse/` 是各家真实接口的原始 SSE 流与错误响应，供 `tests/test_providers.py` 离线回归。协议有变化时重新录制：

```bash
.venv/bin/python tests/live/record_sse.py
```
