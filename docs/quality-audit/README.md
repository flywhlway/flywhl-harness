# flywhl-harness 质量审计

审计日期：2026-10-18。基线：`main` @ `385a0a0`。范围是仓库内的 harness 库、CLI、FastAPI 控制台与离线测试。没有改业务源码，没有做修复。

> **整改状态（2026-10-18）：** Top10（QA-001～006、QA-009～012）已修复并验证，审计套件 13 passed，离线套件 322 passed。
> QA-007、QA-008、QA-013 未处理，理由与核查中发现的同根因变体见 [fix-report.md](fix-report.md)。下文保留审计时的原始结论。
>
> **第二轮（2026-10-31）：**
> - 修复了本轮新发现的问题，包括伪装成只读的执行、仓库配置驱动 git、hook 改写与失败策略、Web 令牌与 CSP；
> - 关闭 QA-007 和 QA-013，QA-008 部分处理；
> - 离线套件 455 passed，审计套件 13 passed，详见 [security-round2.md](security-round2.md)；
> - 过程中在开发机上发生了一次事故，复盘见 [incident-2026-10-31.md](incident-2026-10-31.md)。
>
> **第三轮（2026-11-04）：**
> - 系统沙箱默认开启，QA-008 关闭；
> - 离线套件 macOS 724 passed、Linux 720 passed，干净安装验收两个平台都是 23/23；
> - 33 个 live 场景在真实沙箱里跑过，一次失败没有定位到原因；
> - 复核记录见 [release-readiness.md](release-readiness.md) 末尾，新发现见 [issues.md](issues.md) 文末。
>
> **0.2.0 门禁（2026-11-05）：**
> - 离线套件 macOS 726 passed、Linux 723 passed，33 个 live 场景通过；
> - 结果、例外和没闭环的事见[门禁记录](evidence/release-gate-0.2.0.md)。
>
> **发布流程（2026-11-06）：**
> - 补了[发布检查清单](release-checklist.md)、[SECURITY.md](../../SECURITY.md) 和 Dependabot 配置；
> - 全历史密钥扫描 0 条命中，交叉校验的 4 处都是测试假值，见[扫描记录](evidence/secret-scan-2026-11-06.md)。

## 结论

**不能按生产发布标准放行。** 离线套件 268 项通过，演示控制台的只读任务和审批拒绝闭环也走通了，但默认权限模式挡不住三条已经复现的失败路径：换行把后续命令伪装成只读、macOS 上用大小写别名读凭证、`ps` 读出父进程里已被剥离的密钥。这三条都不需要 bypass，也不需要人点批准。

生产就绪度：**未就绪**。明细见 [release-readiness.md](release-readiness.md)。

## 缺陷统计

| 级别 | 已复现 | 含义 |
|---|---|---|
| P0 | 3 | 默认模式下的命令执行或凭证/密钥进入模型上下文 |
| P1 | 3 | 本地 CSRF、灾难性命令绕过、`model = "mock"` 打到真实供应商 |
| P2 | 5 | 沙箱与 hook 的已知缺口、数据文件权限、移动布局、系统口令文件 |
| P3 | 2 | 文档计数、SQLite 断电耐久 |
| 待验证 | 4 | 见下方，不计入缺陷数 |

P0：QA-001、QA-002、QA-003。P1：QA-004、QA-005、QA-006。

## TOP 风险

1. **QA-001** 命令字符串里的换行不会被拆成独立命令。`echo safe` 加换行再 `touch`，在 plan 模式、`on_ask=deny` 下仍然执行。策略把整段判成只读。
2. **QA-002** 凭证 glob 和 bash 线索都区分大小写。在 APFS 上 `cat .SSH/config`、`read .SSH/config`、`grep` 指向 `.SSH` 都会在 default 模式直接返回 `.ssh/config` 的内容。
3. **QA-003** `ps eww -p $PPID` 在 default 和 plan 下是只读放行。它读的是 harness 进程的环境，剥离逻辑只作用于子进程，挡不住。`echo $MYSQL_PWD` 同样放行，因为变量名不在剥离正则里。
4. **QA-004** 跨站检查只看 Origin 的主机名。`http://127.0.0.1:9999` 可以向 `127.0.0.1:8766` 创建会话；带访问令牌也一样。
5. **QA-005** `bash -lc 'rm -rf ~'` 和 `find ~ -delete` 不是灾难性命令。bypass、`--yes` 会执行。批准一次 `bash -lc 'echo …'` 并选择“本会话始终允许”后，授权规则是 `allow:bash(bash -lc*)`，下一条完全不同的 `bash -lc` 不再询问。

## 交付物

| 文件 | 内容 |
|---|---|
| [architecture-review.md](architecture-review.md) | 架构、数据流、权限、已核对的设计与债务 |
| [test-report.md](test-report.md) | 执行记录、通过/失败、覆盖缺口、追踪矩阵 |
| [issues.md](issues.md) | 缺陷台账 |
| [uat-report.md](uat-report.md) | 隔离环境上的 API 与浏览器验收 |
| [remediation-roadmap.md](remediation-roadmap.md) | 分阶段整改，未获批准前不实施 |
| [release-readiness.md](release-readiness.md) | 发布判定 |
| [evidence/](evidence/) | pytest 输出、UAT 记录、截图；`fix-*` 是整改后的复核 |
| [tests/](tests/) | 安全不变量测试。审计时 **11 failed, 2 passed**，整改后 13 passed |
| [fix-report.md](fix-report.md) | Top10 整改记录：选取理由、根因、修复、回归测试、残余风险 |
| [security-round2.md](security-round2.md) | 第二轮安全加固：修复前后的行为、回归用例、仍未覆盖的缺口 |
| [capability-backlog.md](capability-backlog.md) | 能力扩展清单：本轮已实现的能力，以及按优先级排序的后续项 |
| [incident-2026-10-31.md](incident-2026-10-31.md) | 事故复盘：回归测试在开发机上执行了 `rm -rf ~` |
| [release-checklist.md](release-checklist.md) | 发布检查清单：发布前门禁、发布动作、发布后验证 |

## 待验证（不是已复现缺陷）

- `tests/live` 33 项未跑。需要密钥和费用，本次标 BLOCKED。
- 没有安装覆盖率工具，不报告覆盖率百分比。
  - 2026-11-03 起，CI 的 coverage-audit 作业每次出报告；
  - 基线 81%（语句加分支，macOS），见 [evidence/baseline-coverage-2026-11-03.txt](evidence/baseline-coverage-2026-11-03.txt)；
  - CI 上 Linux 是 82%。差别主要在 `tools/sandbox.py`：Linux 档在 macOS 上不计入，这个文件在 CI 上是 88%，本机是 50%。
  - 2026-11-06 起，这个作业和 [coverage-baseline.json](coverage-baseline.json) 比较，整体或关键模块低超过 0.5 个百分点就失败。不设绝对阈值，也不为抬数字补用例。
- 没有跑依赖漏洞数据库扫描。已记录的运行版本：httpx 0.28.1、fastapi 0.143.0、starlette 1.7.0、uvicorn 0.54.0、pytest 9.1.1。
  - 2026-11-03 首次扫描，发现 certifi 一条（PYSEC-2026-4320），已升级到 2026.10.23，复扫没有已知漏洞；
  - 文档站的 npm 依赖还有 6 条，留待处理；
  - 明细见 [evidence/baseline-deps-audit-2026-11-03.txt](evidence/baseline-deps-audit-2026-11-03.txt)。
- 恶意正则导致的 grep 拒绝服务、符号链接替换的时间窗，只看到代码路径，没有做成稳定复现。第二轮已经复现并修复，见 [security-round2.md](security-round2.md)。

## 复现入口

```bash
.venv/bin/python -m pytest -q --tb=no                  # 268 passed
.venv/bin/python -m pytest docs/quality-audit/tests -q # 预期失败，见 evidence/audit-pytest.txt
```

失败用例断言的是产品声明的安全不变量。修代码之前不要改断言去换绿灯。

耐久浸泡用 mock provider，不联网，子进程和数据都在临时目录里，不达标时退出码非 0：

```bash
uv run python scripts/soak.py sse --json soak-sse.json      # 默认 2 小时 4 个会话：RSS、句柄、SSE 完整性
uv run python scripts/soak.py kill9 --json soak-kill9.json  # 默认 50 轮：kill -9 接管后副作用恰好一次
```

RSS 判定把预热之后的斜率外推到 2 小时。macOS 上 RSS 会跳十几 MiB，窗口只有一两分钟时外推值由噪声决定，要看增长至少跑 10 分钟。

每回合都要读会话的全部历史，所以一个会话一直跑下去时，内存和运行耗时会随历史增长。`--rotate-every N` 每 N 个运行换一个新会话，用来区分这种增长和真正的泄漏，两种结果都要看。短跑只想验证流程时写 `--max-rss-growth-mib 100000`，命令里就看得出这次没有判定 RSS。
