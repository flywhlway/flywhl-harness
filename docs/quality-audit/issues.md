# 缺陷与风险台账

状态只有三种：已复现、潜在风险、待验证。下面 QA-001 至 QA-013 都是已复现。潜在风险和待验证写在文末，不进入 P0–P3 计数。

证据目录：[evidence/](evidence/)。可重复断言：[tests/test_security_invariants.py](tests/test_security_invariants.py)。

整改（2026-10-18）：QA-001～006、QA-009～012 已修复，QA-007、QA-008、QA-013 未处理，见 [fix-report.md](fix-report.md)。下面的“实际”一栏是审计时的结果。

## 索引

| ID | 级别 | 模块 | 一句话 |
|---|---|---|---|
| QA-001 | P0 | `policy/bash_analyzer.py` | 换行后的命令被当成前一个只读命令的参数 |
| QA-002 | P0 | `policy/sensitive.py`、bash 线索 | 大小写别名绕过凭证硬拒绝 |
| QA-003 | P0 | `tools/shell.py`、权限默认值 | 父进程环境可被只读命令读出 |
| QA-004 | P1 | `server/app.py` | Origin 不校验端口，回环上的其他网站能写 API |
| QA-005 | P1 | bash 分析、`grant_for` | 合并短选项和 `find -delete` 不是灾难性命令；会话授权过宽 |
| QA-006 | P1 | `llm/profiles.py` | `model = "mock"` 在有密钥时发给真实供应商 |
| QA-007 | P2 | `config.HookConfig` | 命令 hook 超时默认失败即放行 |
| QA-008 | P2 | `tools/sandbox.py` | 沙箱默认关闭，开启后也不限制网络和钥匙串 |
| QA-009 | P2 | `runtime/store.py` | 会话库文件权限 0644 |
| QA-010 | P2 | `web/` 布局 | 390px 宽度下对话被检查器挤出视口 |
| QA-011 | P2 | 凭证清单 | bypass 下放行 `cat /etc/master.passwd` 和钥匙串查询 |
| QA-012 | P3 | HTTP 令牌 | cookie 无 Secure；查询串令牌对所有路径有效 |
| QA-013 | P3 | README | 离线测试数仍写 240，实际 268 |

---

## QA-001 换行命令绕过 plan 与审批

- **模块：** `policy/bash_analyzer.py` 的分段；执行在 `tools/shell.py`
- **描述：** 分析器用 shlex，换行是空白而不是分段符。bash 把换行当命令分隔符。`echo safe` 换行后再跟任意命令，策略看到的是一条只读 `echo`。
- **触发：** 工具参数 `command` 含真实换行。JSON 工具调用可以带这个字符。
- **复现：**
  1. plan 模式，`on_ask=deny`。
  2. 执行 bash：`echo safe` + 换行 + `touch plan_marker.txt`。
  3. 同时对 `echo safe` + 换行 + `rm -rf subdir` 做 `analyze()`。
- **预期：** plan 模式拒绝；含删除的形式标成灾难性或至少不是只读。
- **实际：** 标记文件被创建。`analyze` 等级为 `read`，`catastrophic` 为 false。default 模式下 `echo` 换行 `python3 -c`、`echo` 换行 `curl http://127.0.0.1:9/` 的策略决策也是 allow。后两条只查了决策，没有发起网络请求。
- **证据：** `docs/quality-audit/tests/test_security_invariants.py` 前两个失败；`evidence/audit-pytest.txt`
- **影响：** 不可信模型或被投毒的仓库提示，可以在默认模式和 plan 模式里写工作区、跑解释器、发网络请求，审批和“plan 是硬上限”都不生效。工作区外的绝对路径有时会因为“路径越界”升级成 ask，相对路径不会。
- **根因：** 分段符集合与 bash 的命令分隔符不一致。路径越界扫描也只扫到被当成参数的词，不会把第二个命令名提升成命令。
- **修复建议：** 在词法阶段把换行（以及 bash 会当成换行的结构）拆成独立段，每段单独分级，整体取最大；无法拆清则拒绝执行，不要降级成 echo。补上 plan 模式执行测试。
- **回归：** 本目录两个换行用例转为通过；既有 `tests/test_security.py` 的包装命令用例保持通过。确认 `echo 'a\nb'` 这种引号内的换行不会误伤。

## QA-002 凭证路径的大小写别名

- **模块：** `policy/sensitive.py` 的 `fnmatchcase`；`CREDENTIAL_HINTS`；文件工具与 bash 都走这套匹配
- **描述：** 清单区分大小写。APFS 默认不区分。`.ssh/config` 与 `.SSH/config` 是同一文件，后者不命中 `*/.ssh/*`，也不命中正则 `\.ssh`。
- **触发：** default 模式，`on_ask=deny`，工作区内存在 `.ssh/config`。
- **复现：**
  1. 写入 canary 到 `.ssh/config`。
  2. `read` 路径 `.SSH/config`。
  3. bash `cat .SSH/config`。
  4. `grep` 路径 `.SSH`，模式为 canary。
- **预期：** 三条都是 deny，工具结果里没有文件内容。小写路径保持 deny。
- **实际：** 三条都是 allow / 工具状态 ok，结果含 `CANARY-SSH-CONFIG`。小写 `.ssh/config` 与 `.ssh/ID_RSA` 仍是 deny。plan、bypass、accept_edits 对 `.SSH/config` 同样 allow。`.Kube/config` 的策略决策也是 allow（没有放真实 kube 文件）。
- **证据：** 同上测试文件的 `test_credential_case_alias_is_denied`。探针没有使用用户主目录里的密钥。
- **影响：** 模型只要改路径大小写，就能在默认模式把工作区内的 SSH 配置、以及用同样手法指向的 `.kube`、`.aws`、`.gnupg` 读进上下文、事件和轨迹。硬拒绝被绕开。
- **根因：** 匹配发生在用户给出的字符串上。`Path.resolve()` 在这台机器上保留调用时的大小写，`fnmatchcase` 对不上清单。
- **修复建议：** 用 `os.path.samefile` 或规范大小写之后再匹配；bash 线索改为大小写不敏感，同时保留路径段边界，避免误伤 `ssh_utils`。文件工具在执行前对解析后的真实路径再查一次 `sensitivity`。
- **回归：** 本用例在不区分大小写的卷上转为 deny；区分大小写的系统应 skip 而不是假装通过。补 `.kube`、`.aws`、`.docker/config.json` 的别名。小写拒绝和现有清单一致性测试保持通过。

## QA-003 只读命令读出进程密钥

- **模块：** `tools/shell.py` 的 `_SECRET_ENV`；`bash_analyzer.READ_CMDS` 含 `echo`、`printenv`、`ps`
- **描述：** 剥离只作用于 bash 子进程的环境。`ps eww -p $PPID` 打印的是 harness 进程。另外，`MYSQL_PWD`、`SMTP_PASS`、`GITHUB_PAT` 这种名字不匹配现有正则，子进程里也还在。
- **触发：** default 或 plan，不需要批准。
- **复现：** 在只含 canary 的环境里启动 Python，调用 `BashTool`。命令分别为 `echo "$MYSQL_PWD|$DEEPSEEK_API_KEY"`、`printenv MYSQL_PWD`、`ps eww -p $PPID`。策略层对这三条在 default 和 plan 下的决策另测。
- **预期：** 父进程密钥不出现在工具结果里；读取进程环境的命令至少是 ask，凭证类名字在子进程中为空。
- **实际：** `echo` 打出 mysql canary，没有打出 key canary。`ps` 两者都打出。三条命令的策略决策都是 allow（plan 为“只读放行”）。
- **证据：** `test_parent_env_dump_is_not_auto_allowed`、`test_unlisted_secret_names_do_not_reach_the_model`。完整 `ps` 输出没有落盘。
- **影响：** 提示注入或模型主动调用，可以把 API 密钥写进会话库和轨迹。产品注释里的“模型即使执行 env 也看不到原始凭据”对 `ps` 不成立。`env` 不带参数会被升成 external，这条路径是只读命令。
- **根因：** 信任边界画在子进程环境上，观察父进程的命令仍被归进 `READ_CMDS`。剥离名单是子串正则，不是“可能是秘密”的默认拒绝。
- **修复建议：** `ps` 带环境相关参数、`printenv`、`echo`/`printf` 展开未剥离变量，都不要自动放行。剥离改为默认去掉未知的大写敏感后缀，并显式覆盖 `MYSQL_PWD`、`*_PAT`、`SMTP_PASS`、`DATABASE_URL` 这类名字。沙箱的 `(allow default)` 仍然允许看进程，所以不能只依赖 Seatbelt。
- **回归：** canary 测试里 mysql 与 key 都不出现；`echo hello`、`ps` 不带环境参数仍可按只读放行。不要用开发者的真实环境做断言输出。

## QA-004 回环端口上的跨站写请求

- **模块：** `server/app.py` 中间件，`_hostname()`
- **描述：** 改变状态的请求若带 Origin，只要求主机名在白名单里。`127.0.0.1`、`localhost`、`::1` 都在白名单。端口被丢掉。
- **触发：** 服务监听 `127.0.0.1` 且未配令牌（CLI 对回环地址的默认行为）。攻击页来自同一主机的其他端口。
- **复现：**
  1. TestClient，`Host: 127.0.0.1`，`Origin: http://127.0.0.1:9999`，`POST /api/sessions`。
  2. 同一请求加上 `Authorization: Bearer`，服务创建时配置了令牌。
  3. 对照：`Origin: https://evil.example`、`Origin: null`、`Host: evil.example`。
  4. 对真实监听的 `127.0.0.1:8766` 用 urllib 再做一次其他端口的 POST。
- **预期：** 其他端口 403。跨站和 `null` 403。坏 Host 403。
- **实际：** 其他端口两次都是 200（无令牌、有令牌）。对照三项是 403。真实服务上 `127.0.0.1:9999` 也是 200，并因此留下会话 `csrf-other_port`。
- **证据：** 两个 HTTP 失败用例；`evidence/uat-api.json` 的 `csrf` 字段。
- **影响：** 用户机器上任意 `127.0.0.1` 网站可以对 harness 建会话、`POST` 运行、把模式改成 bypass、批准挂起的操作。SameSite=Strict 挡的是跨站，不是同一主机的不同端口，所以令牌 cookie 也会带上。远程网页（主机名不是回环）被现有检查挡住了。
- **根因：** 比较的是主机名，不是 `scheme + host + port`。
- **修复建议：** 写请求的 Origin 必须与本次请求的 Host（含端口）一致，或与服务实际监听的 origin 列表一致。`*` Host 模式下也不能只比主机名。
- **回归：** 本目录两个端口用例改为 403；跨站、`null`、坏 Host 保持 403；浏览器从页面自己的 origin 正常使用。

## QA-005 灾难性命令的另外两种写法，以及过宽的会话授权

- **模块：** `bash_analyzer.py`；`permissions.grant_for`
- **描述：** `-lc` 不是独立的 `-c`，内层脚本不会递归分析，命令落进“未知命令 bash，按可写处理”。`find ~ -delete` 只是“find 带 -delete”加上路径越界，不是灾难性。`grant_for` 对 bash 取前两个词加 `*`。
- **触发：** bypass，或 `--yes`（`on_ask=allow`），或用户对一条 `bash -lc` 选择“本会话始终允许”。
- **复现：**
  1. `analyze("bash -lc 'rm -rf ~'")`，再在 bypass 下 `evaluate`。
  2. 对 `find ~ -delete` 做同样的事。
  3. default 模式，审批回调对第一条 `bash -lc 'echo one > grant_a.txt'` 返回 remember。第二条是另一段 `bash -lc 'echo two > grant_b.txt'`。
- **预期：** 前两条在任何模式 deny。第二条 `bash -lc` 仍要单独审批。
- **实际：** 两条都不是 catastrophic。bypass 决策是 allow。审批只出现 1 次，规则形态为 `allow:bash(bash -lc*)`，`grant_b.txt` 被写上。没有执行 `rm` 或 `find -delete`。
- **证据：** 参数化失败用例和 grant 用例。
- **影响：** “任何模式都拒绝删家目录”对这两种写法不成立。记住一次看似无害的 `bash -lc echo` 之后，同一会话里后续 `bash -lc` 不再询问。default 模式下不 remember 时，`bash -lc 'rm -rf ~'` 是 ask，仍靠人拒绝。
- **根因：** 短选项必须整词等于 `-c`。`find` 的 `-delete` / `-exec` 没有再看目标是不是 `/` 或 `~`。授权用的是 shell 未解析的前两词前缀。
- **修复建议：** 识别 `-[^-]*c` 这种组合短选项并递归；`find` 的删除动作若目标是家目录、根或工作区外，标灾难性。会话授权不要用前两词 glob，至少绑定完整脚本哈希，或对 shell `-c` 禁用 remember。
- **回归：** 两条灾难性用例 deny；grant 用例看到两次审批。`bash -c 'git status'` 仍按内层命令分级。`find . -name x` 保持只读。

## QA-006 `model = "mock"` 不会强制演示模型

- **模块：** `llm/profiles.py` `resolve()`；`examples/11_kill9_cli_recover.py` 的环境清洗
- **描述：** 配置值 `mock` 不在 `MODELS` 表里。`resolve` 把它当成模型名，供应商改由 `detect_profile()` 决定。环境里第一个配了密钥的供应商会接住这个名字。
- **触发：** `flywhl.toml` 写 `model = "mock"`，进程环境里有 `DEEPSEEK_API_KEY` 或其他供应商密钥。示例 11 只去掉 `FLYWHL_*` 和 `LLM_*`。
- **复现：**
  1. `monkeypatch` 设置 `DEEPSEEK_API_KEY=qa-not-a-real-key`，调用 `resolve("mock", "auto")`。
  2. 在带密钥的 shell 里跑 `examples/11_kill9_cli_recover.py`。
  3. `env -i` 后再跑同一示例。
- **预期：** 供应商是 `mock`，示例不访问网络，七条不变量通过。
- **实际：** `resolve` 返回 `deepseek`。示例第一次失败，上游 HTTP 400，错误说明传入的模型名是 `mock`，并列出 deepseek 的模型名。Run 为 `failed`，通知数 0。`env -i` 后七条不变量通过。
- **证据：** `test_model_mock_stays_on_demo_provider_when_a_key_exists`；示例的两次终端输出（密钥值未记录）。
- **影响：** 文档和 CI 认为这个示例不需要密钥、结果确定。开发者机器上它会打到真实接口并失败，恢复演示没有被执行。若上游真有一个叫 mock 的计费模型，还会产生费用。
- **根因：** `mock` 是供应商名，配置字段却放在 `model`。自动探测优先于“这个名字是不是内置演示”。示例的干净环境没有去掉 `*_API_KEY`。
- **修复建议：** `model` 为 `mock` / `flywhl-demo` / `scripted` 时固定 `DemoProvider`，不要把字面量 `mock` 发给外部。示例 11 的子进程环境改为白名单。
- **回归：** 有假密钥时 `profile.name == "mock"`；`env` 里保留密钥时示例 11 仍通过且不出现出站请求。

## QA-007 命令 hook 超时默认放行

- **级别：** P2。既有测试 `test_command_hook_is_killed_on_timeout` 把“超时只是警告”写成了预期。
- **描述：** `HookConfig.fail_closed` 默认 false。PreToolUse 命令 hook 若被当成安全策略，超时或执行失败不会阻断工具。
- **触发：** hook 命令超过 `timeout`。
- **预期（若 hook 是强制控制）：** 失败即拒绝。
- **实际：** 警告，工具继续走权限流程。
- **影响：** 项目配置里用 hook 做最后一道拒绝时，卡住 hook 就能绕过它。权限层仍在，所以这不是 QA-001 那种直接执行。
- **根因：** 为了和“hook 挂了不要卡死代理”做的取舍，默认方向是可用性。
- **修复建议：** 安全相关 hook 默认 `fail_closed=True`，或在信任提示里写明超时会放行。
- **回归：** 超时用例在 fail_closed 时工具状态为 denied。

## QA-008 沙箱默认关闭，且原型不封网络

- **级别：** P2。代码注释已声明，默认配置没有打开。
- **描述：** `PermissionConfig.sandbox` 默认 `off`。`macos` 档从 `(allow default)` 出发，拒绝凭证路径和大部分工作区外写入，不限制网络、进程和 Mach 服务。命令 hook 与 MCP 子进程不进沙箱。
- **影响：** QA-001 的网络请求、QA-003 的 `ps`、QA-011 的钥匙串查询，即使用户打开了 macos 档也不在 Seatbelt 的拒绝范围内。文件类破坏会好一些。
- **修复建议：** 不可信工作区的默认档不要是 `off`。网络和进程查看要有独立拒绝，不能只靠命令字符串。
- **回归：** 打开沙箱后，工作区外写入失败；凭证路径读取失败；并增加一条“`ps eww -p $PPID` 看不到父进程密钥”的测试。最后一条在当前 Seatbelt 配置下会失败，应用来驱动规则收紧。

## QA-009 会话数据库对同机其他用户可读

- **级别：** P2。
- **复现：** UAT 工作区 `/tmp/flywhl-qa-uat/ws/.flywhl/harness.db` 与 `-wal`、`-shm` 的权限是 `0644`。
- **预期：** 仅属主可读写。库里会有提示词、工具输出，QA-003 一旦发生还会有密钥。
- **实际：** 组和其他用户可读。
- **根因：** 沿用进程 umask，创建连接时没有 `chmod 0600`。
- **修复建议：** 创建 `harness.db` 和数据目录后把权限收到 `0700` / `0600`。
- **回归：** 临时目录里新建 Store，断言模式。

## QA-010 手机宽度下检查器挤掉对话

- **级别：** P2。
- **复现：** 会话页，视口 390×844，检查器处于默认打开。截图 `evidence/uat-mobile-session.png`。
- **预期：** 对话可完整阅读；检查器改为抽屉或收到对话下方。
- **实际：** 检查器占住左侧一列，对话只剩一条缝，右侧大块空白。桌面宽度正常，见 `evidence/uat-list-dir.png`。
- **影响：** 窄窗口和手机无法完成审批或阅读工具结果。没有做多浏览器和横屏。
- **修复建议：** 低于断点时检查器不要参与并排 flex，或默认关闭。
- **回归：** 在 390px 下断言对话区域宽度大于视口的一半。本次没有把这条写进 pytest。

## QA-011 bypass 可以读 macOS 口令库和钥匙串

- **级别：** P2。default 模式是 ask，不是自动执行。
- **复现：** 只做策略评估，没有读真实文件，也没有调用 `security`。
  - `cat /etc/master.passwd`：bypass allow，plan deny，default ask。不是凭证硬拒绝。
  - `security find-generic-password -w -s demo`：等级 dangerous，bypass allow。
- **预期：** 与 `/etc/shadow` 同一档，任何模式 deny。钥匙串查询至少不随 bypass 自动执行，或被沙箱挡住。
- **实际：** 清单只有 `*/etc/shadow`。`security` 是未知命令，靠 `SECRET_HINTS` 升到 dangerous，bypass 不再升级。
- **影响：** bypass 或 `--yes` 会尝试读本机口令库（通常还需 root）或导出钥匙串口令。沙箱注释写明不限制 Mach 服务。
- **修复建议：** 把 `master.passwd` 和 `security find-*-password` 放进硬拒绝。沙箱增加对应的 deny。
- **回归：** 两条在 bypass 下 deny。不要在测试里打开真实口令文件。

## QA-012 令牌出现在查询串，cookie 不带 Secure

- **级别：** P3。
- **复现：** `GET /?token=qa-token` 不跟随重定向。`Set-Cookie` 为 `flywhl_token=qa-token; HttpOnly; Path=/; SameSite=strict`，没有 `Secure`。中间件在任意路径接受 `?token=`。
- **影响：** 回环 HTTP 上 Secure cookie 本来就不能用，这一点可以保留。查询串令牌会进访问日志、浏览器历史和 Referer。只有打开 `/` 会 303 清掉地址栏。
- **修复建议：** 查询串令牌只在 `/` 接受一次并立刻失效；其他路径只认 cookie 或 `Authorization`。
- **回归：** `/api/info?token=` 在仅有查询串、没有 cookie 时为 401。

## QA-013 README 的测试计数过期

- **级别：** P3。
- **实际：** README 写 240 项离线测试、2026-10-16 的复现。2026-10-18 本机是 268 passed、33 deselected。
- **修复建议：** 改成“以 pytest 收集结果为准”，或在改测试时同步数字。不要把行数或用例数写成质量门禁。

---

## 潜在风险（未单独复现成稳定失败）

| 项 | 说明 |
|---|---|
| grep 灾难性回溯 | `GrepTool` 直接 `re.compile` 用户模式。没有用超时样本跑挂起，不标缺陷 |
| 符号链接替换 | 先 `resolve` 再读，中间可以换链接目标。没有做成可靠的时间窗实验 |
| 审批后的 hook 再改参数 | 执行器在审批后若 hook 再次给出 `updated_input`，没有看到第二次 deny 检查。hook 来自已信任配置，未做成端到端用例 |
| `0.0.0.0` 关闭 Host 白名单 | CLI 在非回环监听时把 `*` 放进允许列表，只剩共享令牌。没有在局域网起服务去打 |

## 待验证

| 项 | 原因 |
|---|---|
| live 33 项 | 未运行。BLOCKED：费用和密钥 |
| 覆盖率数字 | 未安装 coverage |
| 依赖 CVE | 未跑漏洞库 |
| 容量与长时间 SSE | 只有 30 次空载 `GET /api/info` |
