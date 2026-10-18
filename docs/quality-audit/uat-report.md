# 真实业务 UAT

环境与生产数据隔离：`env -i` 启动 `flywhl serve -w /tmp/flywhl-qa-uat/ws --host 127.0.0.1 --port 8766`，`FLYWHL_HOME=/tmp/flywhl-qa-uat/home`。没有加载仓库 `.env`。供应商是内置 `mock` / `flywhl-demo`，权限模式 default。审计结束时进程已停。记录见 [evidence/uat-api.json](evidence/uat-api.json)。

浏览器是 Cursor IDE 浏览器（Chromium 内核）。本机没有 Playwright 的 Python 包。插件自带的 Playwright 在打开 `8766` 之后，标签被切到了另一个本地应用（SkillOps 控制台），那部分结果作废，没有拿它当 flywhl 的验收。没有保存 trace。

## 场景

| 场景 | 操作 | 结果 |
|---|---|---|
| 服务健康 | `GET /api/info` | 200。version 0.1.0，12 个工具，工作区是临时目录 |
| 只读闭环 | 开始页点“浏览工作区”（模板会填入“列出目录”并提交） | 会话标题变为“列出目录”。工具 `ls .` 只读、完成、0 ms。检查器状态：已创建 → 排队 → 运行 → 已完成。截图 `evidence/uat-list-dir.png` |
| 写操作审批 | 新会话点“创建文件” | 出现审批卡：`write(hello.py)`，中等风险，unified diff，可编辑 JSON。导航上审批计数为 1。截图 `evidence/uat-approval.png` |
| 拒绝后不落盘 | 点“拒绝…”，再点“拒绝操作” | API 中该 Run 变为 completed，pending 审批为 0。界面上 write 为“已拒绝”。演示模型接着 `read hello.py`，0 ms 出错。临时工作区只有 `.flywhl/`，没有 `hello.py` |
| 跨站 | 真实端口上 `Origin: https://evil.example` 与 `null` | 403 |
| 同主机其他端口 | `Origin: http://127.0.0.1:9999` 创建会话 | 200。侧边栏留下 `csrf-other_port`。这是 QA-004，不是 UI 误显示 |
| 空载延迟 | info 连续 30 次 | p50 1.04 ms，p95 1.92 ms。不能外推容量 |
| 手机宽度 | 390×844，检查器开着 | 对话被挤出视口，右侧空白。截图 `evidence/uat-mobile-session.png`。QA-010 |

静态资源和轮询的 `/api/sessions`、`/api/approvals` 在性能条目里是 200。SSE `/events` 的 `responseStatus` 为 0，连接还开着，不是错误页。导航前没有挂上 console 监听，**不能声称没有 console error**。页面上没有错误横幅。

## 没有走完的业务

- 没有点“批准”，所以没有验证批准后的文件内容与 diff 一致。拒绝路径证明了不批准不会写文件。
- 没有在 UI 里切换 bypass、暂停或停止一个正在跑的工具。按钮存在，未点击。
- 没有跑子代理、压缩、循环检测这几个演示模板。
- 没有真实模型。演示模型是规则脚本，不能代表供应商协议。
- 多角色、多租户：产品没有账号体系，无法构造。用“另一个本地端口上的页面”代替了越权调用，结果是 QA-004。

## 数据清理

临时工作区在 `/tmp/flywhl-qa-uat`，不在用户项目里。服务已停止。仓库 `.flywhl` 和 `~/.flywhl` 没有被这次 UAT 写入（`FLYWHL_HOME` 指向临时目录）。审计测试使用 pytest 的 `tmp_path` 或内存 SQLite。
