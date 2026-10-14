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
