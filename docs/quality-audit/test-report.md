# 测试执行报告

环境：macOS（darwin 25.6.0），Python 3.12.13，仓库 `.venv`。审计过程没有修改 `src/`。

## 1. 既有离线套件

```text
.venv/bin/python -m pytest -q --tb=no
268 passed, 33 deselected in 21.10s
```

记录：[evidence/offline-pytest.txt](evidence/offline-pytest.txt)。33 项是 `tests/live`，`pyproject.toml` 的 `addopts` 默认排除。CI（`.github/workflows/tests.yml`）同样不跑 live，矩阵是 Ubuntu 3.11/3.12/3.13 和 macOS 3.12。本次没有复跑 CI。

README 仍写“240 个离线测试”。当前收集结果是 268。这是文档漂移（QA-013），不是测试失败。

没有 pytest-cov，**不报告覆盖率百分比**。从测试文件分布看，loop、生命周期、取消、恢复、策略、供应商协议、运行时都有直接测试；`server/app.py` 和 `web/` 没有对应的离线测试文件，HTTP 行为是这次审计补测的。

## 2. 安全不变量套件

```text
.venv/bin/python -m pytest docs/quality-audit/tests -q --tb=line
11 failed, 2 passed in 0.46s
```

完整输出：[evidence/audit-pytest.txt](evidence/audit-pytest.txt)。

| 结果 | 用例 | 对应问题 |
|---|---|---|
| 失败 | `test_newline_is_not_a_read_in_plan_mode` | QA-001。plan 模式决策是 allow，断言停在执行前。同条件探针已创建 `plan_marker.txt` |
| 失败 | `test_newline_rm_is_catastrophic` | QA-001。`echo` 换行 `rm -rf subdir` 的等级不是 read 以外的灾难标记，分析等级为 read |
| 失败 | `test_parent_env_dump_is_not_auto_allowed` | QA-003。`ps eww -p $PPID` 在 default 下 allow |
| 失败 | `test_unlisted_secret_names_do_not_reach_the_model` | QA-003。子进程 `echo` 打出 `MYSQL_PWD` canary；`DEEPSEEK_API_KEY` canary 没有打出 |
| 失败 | `test_credential_case_alias_is_denied` | QA-002。`os.path.samefile` 确认 `.SSH/config` 与 `.ssh/config` 是同一文件，default 仍 allow |
| 失败 | `test_destructive_forms_are_denied_in_bypass` 两条 | QA-005。`bash -lc 'rm -rf ~'`、`find ~ -delete` 的 `catastrophic` 为 false |
| 失败 | `test_session_grant_does_not_authorize_a_different_lc_script` | QA-005。只收到 1 次审批，第二条 `bash -lc` 已执行 |
| 失败 | `test_loopback_other_port_is_rejected` | QA-004。Origin `http://127.0.0.1:9999` 返回 200 |
| 失败 | `test_token_does_not_widen_origin` | QA-004。Bearer 令牌下同一 Origin 仍是 200 |
| 失败 | `test_model_mock_stays_on_demo_provider_when_a_key_exists` | QA-006。注入假的 `DEEPSEEK_API_KEY` 后 `resolve("mock")` 得到供应商 `deepseek` |
| 通过 | `test_cross_site_origin_is_rejected` | `https://evil.example` → 403 |
| 通过 | `test_foreign_host_is_rejected` | `Host: evil.example` → 403 |

这些失败是缺陷证据。修复实现之后再跑，目标是全部通过。

另外用脚本化模型直接执行过、但没有放进上面文件的探针：

- default + `on_ask=deny` 下，`echo safe\ntouch newline_marker.txt` 创建了标记文件。
- bypass 下 `bash -lc 'echo ran > lc_marker.txt'` 创建了标记文件。没有执行 `rm`。
- default + `on_ask=deny` 下，`read .SSH/config`、`bash cat .SSH/config`、`grep` 路径 `.SSH` 的工具结果都含 canary `CANARY-SSH-CONFIG`。
- 干净子进程环境里 `ps eww -p $PPID` 的输出同时含被剥离的 key canary 和 `MYSQL_PWD` canary。输出没有写入证据文件。

## 3. 崩溃接管

`examples/11_kill9_cli_recover.py` 跑了两次。

继承当前 shell 环境时失败：进程 A 的模型调用返回 HTTP 400，上游称支持的模型是 `deepseek-flash` 与 `deepseek-v4-pro`，实际传入的名字是 `mock`。Run 停在 `failed`，通知数 0，七条不变量里五条失败。这是 QA-006，不是恢复状态机本身坏了。

`env -i` 去掉供应商密钥后再跑，七条不变量全部通过：`running → interrupted → queued → running → completed`，外部通知数 1，步骤 `reconciled`。

## 4. 接口与浏览器

隔离服务：`127.0.0.1:8766`，工作区和 `FLYWHL_HOME` 都在 `/tmp/flywhl-qa-uat`，`env -i` 启动，供应商 `mock` / `flywhl-demo`。服务在审计结束后已停。

| 检查 | 结果 |
|---|---|
| `GET /api/info` 30 次 | p50 1.04 ms，p95 1.92 ms，max 30.06 ms。这是本机空载，不是容量测试 |
| 创建会话并 `列出目录` | Run `completed` |
| `创建 hello.py` | 审批 `write`，risk medium，带 preview；拒绝后 `hello.py` 不存在 |
| Origin `evil.example` / `null` | 403 |
| Origin `127.0.0.1:9999` | 200（QA-004，与 TestClient 一致） |
| 静态路径穿越 | 404 |

浏览器走的是 Cursor 自带浏览器，不是本机未安装的 Playwright Python 包。插件 Playwright 打开页面后，标签被导航到了另一个本地应用，随后的操作改在独立标签上完成。没有保存 Playwright trace。截图在 `evidence/uat-*.png`。过程见 [uat-report.md](uat-report.md)。

## 5. 明确没做的测试

| 项 | 状态 | 原因 |
|---|---|---|
| `tests/live` 33 项 | BLOCKED | 要真实模型和密钥，README 写明不进 CI，本次不花钱跑 |
| 语句覆盖率 | BLOCKED | 环境里没有 coverage / pytest-cov |
| 依赖 CVE 扫描 | BLOCKED | 没有 pip-audit / osv-scanner |
| 并发压测、慢查询、长时间 SSE 积压 | 未做 | 只测了空载 info；SQLite 是单连接写入，容量边界没有数据 |
| 多浏览器矩阵 | 未做 | 只用了一个 Chromium 内核的 IDE 浏览器 |
| 破坏性命令的真实执行 | 故意不做 | `rm -rf ~`、`find ~ -delete` 只断言分析器和策略，用 `touch` / `echo` 证明同类语法会执行 |

## 6. 需求—功能—测试—缺陷

| 功能 | 既有离线测试 | 本次 | 缺陷 |
|---|---|---|---|
| 主循环、工具错误、并行只读 | 有 | 演示模型 UI 走通只读 | — |
| 上下文压缩、预算 | 有 | 未再跑 | — |
| 取消、暂停、恢复 | 有 | UI 上看到暂停/停止按钮，没有点中途取消 | — |
| kill -9 接管与幂等通知 | 有，示例 11 | 无密钥时通过，有密钥时失败 | QA-006 |
| 四种权限模式、灾难性命令、凭证清单 | 有，覆盖常见包装 | 换行、大小写、`-lc`、`find -delete` 失败 | QA-001 QA-002 QA-005 |
| 密钥不进子进程 | 部分（名字匹配 KEY 等） | 剥离对 `KEY` 有效，对 `ps` 和 `MYSQL_PWD` 无效 | QA-003 |
| 污点、hook、审批改参 | 有 | UI 审批拒绝闭环通过 | — |
| Host / Origin / 令牌 | 未见专门测试 | 跨站和坏 Host 通过，同主机不同端口失败 | QA-004 |
| Web 控制台 | 无自动化 | 只读、审批、拒绝、手机宽度 | QA-010 |
| 多租户 / RBAC / IDOR | 不适用 | 产品没有这层模型 | 发布范围外，见就绪度 |
| 真实模型协议 | live，未跑 | BLOCKED | — |
