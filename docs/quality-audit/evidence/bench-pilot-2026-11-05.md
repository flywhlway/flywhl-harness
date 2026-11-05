# 公开基准真实模型试跑记录

日期：2026-11-05。这是开跑前的试跑，只用来量单价、查流水线和 harness 的问题，**数据不计入基准结果**。环境是一次性 Linux 容器，不挂宿主目录，真实 bubblewrap 0.12.0，容器里只有被测模型的密钥。

## 第一轮：五家各跑 24 个场景

提交 `1db66af`，花费 0.39 美元。

| 模型 | 过 | 败 |
|---|---:|---:|
| `deepseek:deepseek-flash` | 24 | 0 |
| `dashscope:qwen3.8-flash` | 24 | 0 |
| `kimi:kimi-k2.6` | 22 | 2 |
| `minimax:MiniMax-M2.7` | 21 | 3 |
| `openrouter:anthropic/claude-haiku-5.5` | 19 | 5 |

10 个失败逐条查了轨迹和接口的实际行为：

| 用例 | 模型 | 原因 | 分类 | 处理 |
|---|---|---|---|---|
| `test_compaction_preserves_key_facts` | Kimi | 第二次压缩没拿到摘要，退回的确定性摘要不带上一次的内容，暗号跟着丢了 | **harness 缺陷** | 退回时带上上一次的摘要 |
| 同上 | MiniMax | 模型回了一句“我来读取 log2.txt 的内容。”，这句话被当成摘要存下来 | **harness 缺陷** | 不分小节的输出不算摘要 |
| `test_error_classification_on_real_endpoints` | Haiku | OpenRouter 用 400 加 `not a valid model ID` 表示模型不存在，没识别出来 | **harness 缺陷** | 补上这个特征 |
| `test_fallback_from_missing_model_to_live_model` | Haiku | 同上，降级链不接手 | **harness 缺陷** | 同上 |
| `test_prefix_cache_hits_on_repeated_prefix` | Haiku | 不带 `cache_control` 就不缓存。实测带上后第二次起命中 7823 token | **harness 缺陷** | 经 OpenRouter 调 Claude 时带上 |
| `test_cancel_mid_stream_then_continue_session` | MiniMax | 提示让模型先开子代理调研，超时前没有流式文本 | 用例过严 | 提示写明不调工具 |
| 同上 | Kimi | 取消后模型接着写文章，超过 240 秒 | 用例过严 | 提示写明不用再写 |
| `test_multi_step_read_task_keeps_reasoning` | Haiku | 用 `bash` 读文件，用例要求必须是 `read` | 用例过严 | `bash` 也算 |
| `test_thinking_switch_is_honoured` | MiniMax | 这家没有思考开关 | 模型能力 | 跳过 |
| 同上 | Haiku | 开了思考也不返回推理，实测 `reasoning_tokens` 为 0 | 模型能力 | 跳过 |

## 第二轮：修复之后

提交 `d338d16`，花费 0.27 美元，脱敏命中 0。

| 模型 | 过 | 跳过 | token | 费用 | 耗时 |
|---|---:|---:|---:|---:|---:|
| `deepseek:deepseek-flash` | 24 | 0 | 266,225 | $0.0281 | 180 秒 |
| `dashscope:qwen3.8-flash` | 24 | 0 | 255,647 | $0.0184 | 305 秒 |
| `kimi:kimi-k2.6` | 24 | 0 | 173,348 | $0.1398 | 530 秒 |
| `minimax:MiniMax-M2.7` | 23 | 1 | 181,482 | $0.0479 | 292 秒 |
| `openrouter:anthropic/claude-haiku-5.5` | 23 | 1 | 311,516 | $0.0347 | 207 秒 |

单次通过不等于稳定。正式的 3 次试验才能回答这个问题。

## Q2 走通

提交 `642f703`，`deepseek-flash`，4 个任务，每种配置各 1 次，花费 0.07 美元。

- 8 条真实模型轨迹回放，分叉 0。脱敏命中 0。
- 普通任务 3 个，两种配置都通过。
- `env-git-hook` 开沙箱时失败：模型转用 `write` 工具写了 hook，但没有可执行权限，用满 30 轮，0.046 美元。关沙箱时 10 轮通过。沙箱敏感任务比普通任务贵，这也是方法说明要观察的代价。

4 个任务看不出配置之间的差异，这里不下结论。

## 费用估算

Q1 每轮 0.27 美元，3 次约 0.8 美元。Q2 共 180 次试验，普通任务按单次 0.004 美元、沙箱敏感任务按开沙箱 0.05 美元估算，约 1.5 美元。合计约 2.5 美元，远低于总额上限 50 美元。

试跑和门禁累计花费约 0.8 美元。
