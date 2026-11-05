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
- 测试报告末尾打印每个场景的 token、缓存命中与费用。用 deepseek-flash 跑完全部 33 个场景约 3～5 分钟、28 万 token、0.03 美元左右
  （其中 64% 的输入命中前缀缓存）。
- 断言只针对 harness 的可观测结果（工具序列、文件内容、Run 状态、事件），提示词要求原样输出时才匹配文字。
  模型偶尔不按要求做时，失败信息里附带完整对话记录（`transcript()`），可以直接判断是 harness 的问题还是模型的问题。

设了 `FLYWHL_LIVE_ARTIFACTS=<目录>` 时，每个用例的结果写进该目录的 `results.jsonl`，轨迹写进 `trajectories.jsonl`。公开基准（`bench/`）用它们出数据。

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
| `LLM_PRIMARY_MODEL`、`LLM_FALLBACK_MODEL`（及链上每一家的密钥） | 项目 `.env` 配置的降级链 |

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

## 失败分类记录（2026-11-04）

系统沙箱默认开启后的回归。在不挂载宿主目录的一次性 Linux 容器里运行，被测的 bash 跑在真实的 bubblewrap 里
（`FLYWHL_TEST_SANDBOX=off` 关掉测试自己的外层沙箱，`FLYWHL_TEST_REAL_SANDBOX=1` 让被测代码用真沙箱）。

同一份代码完整跑了三次，每次都核对过日志里没有密钥：

| 次序 | 结果 | 失败项 | 分类 |
|---|---|---|---|
| 1 | 31 passed，2 failed | `test_configured_fallback_chain_from_env`：运行脚本漏传了 `LLM_PRIMARY_MODEL` | 运行环境，补上后连跑 3 次通过 |
| | | `test_multi_step_read_task_keeps_reasoning`：模型这一轮没有返回推理内容 | 模型随机性，连跑 3 次通过 |
| 2 | 31 passed，2 failed | `test_steer_mid_run_is_followed`、`test_pause_and_resume_at_safe_point`：Run 结束了也没等到期望的事件 | **未定位** |
| 3 | 33 passed | 无 | 278478 token，0.0282 美元，299 秒 |

第 2 次的两条，当时的断言没有写 Run 的状态，所以不知道是模型换了做法（比如用 `bash` 的 `sleep` 而不是 sleep 工具）还是供应商瞬时出错，这两种都只是推测。
能确认的是：这两条单独连跑 6 次、连同前面的取消用例连跑 8 次都通过，和前一条用例无关，也不是必现的 harness 缺陷。
`wait_event` 现在会带上 Run 的状态、错误、最近的事件和对话记录，再出现时可以直接判断。

压缩用例在这轮之前有过一次分类。沙箱开和关的对照各跑 3 次，开时 3 败，关时 2 败 1 过，所有工具调用都成功，没有一次被沙箱拦下：
日志每行几乎一样，模型读完 `log0` 后改用 `wc`、`grep`、`head` 抽查，上下文涨不到阈值。和沙箱无关，所以把窗口从 24000 调到 14000，之后连跑 3 次通过，上面三次完整运行里也都通过。

## 失败分类记录（2026-11-05）

0.2.0 候选版本的回归，环境和 2026-11-04 相同。同一天完整跑了 4 次，前 3 次在修复拒绝理由之前，第 4 次是最终候选：

| 次序 | 结果 | 失败项 | 分类 |
|---|---|---|---|
| 1 | 32 passed，1 failed | `test_rejection_reason_steers_the_model`：out.txt 还是被写入了 | **未定位**，当时断言不带对话记录。夹具已改成看整段参数，之后没有再见到这种失败 |
| 2、3 | 33 passed | 无 | |
| 4 | 33 passed | 无 | 282938 token，0.0389 美元，176 秒，提交 `00abba7` |

同一条用例单独连跑时又暴露出另一个问题，这个已经定位，分类是 **harness 缺陷**：审批时人写的理由放在工具结果里，而系统提示词说工具结果是数据，模型就把用户的指示当成不可信内容。修复前 7 次内失败，理由改走提醒通道后连跑 20 次通过。

11-04 第 2 次的两条（插话、暂停恢复）这 4 次都没有复现，原因仍未定位。

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
