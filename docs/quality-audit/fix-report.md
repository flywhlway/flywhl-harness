# Top10 整改记录

日期：2026-10-18。基线：`main` @ `385a0a0`。逐项在本机（macOS，APFS 不区分大小写）复现后修复；每条都先写出在旧代码上失败的回归测试，再改实现。

## 结论

| 指标 | 整改前 | 整改后 |
|---|---|---|
| 审计安全不变量套件 `docs/quality-audit/tests` | 11 failed, 2 passed | **13 passed** |
| 离线套件 `pytest -q` | 268 passed | **322 passed**（新增 54 个用例；既有断言未改，只在参数表里追加样例） |
| Python 3.11 / 3.13（`uv sync --locked`） | 未跑 | 均为 322 passed、13 passed |
| 示例 11，环境里留着供应商密钥 | 失败，五条不变量不成立 | 七条不变量全部通过 |

[release-readiness.md](release-readiness.md) 的四个放行条件逐条复核结果见该文末尾“整改后复核”。证据：[evidence/fix-pytest.txt](evidence/fix-pytest.txt)、[evidence/fix-uat.json](evidence/fix-uat.json)、[evidence/fix-qa010-mobile-inspector.jpg](evidence/fix-qa010-mobile-inspector.jpg)。

## 选取

P0、P1 全部纳入（QA-001 至 QA-006）。P2 里属于实现缺陷的三条纳入（QA-009、QA-010、QA-011）。第十条取 QA-012：P3，但它是一条确定的令牌泄露路径，修复面小。

不纳入的三条：

| ID | 理由 |
|---|---|
| QA-007 hook 超时放行 | 有意的取舍，与 Claude Code 的 hook 语义一致；`fail_closed = true` 可以显式打开。改默认值会改变所有现有配置的行为，需要产品决定 |
| QA-008 沙箱默认关闭 | 默认开启涉及 Linux 不可用、可用性与网络策略，属于产品决定。本次确认：Seatbelt 按真实路径匹配，`.SSH/config` 在 `macos` 档内本来就读不到 |
| QA-013 README 计数 | 文档漂移。本次测试数又变了，建议按审计意见改成“以 pytest 收集结果为准”时一并处理 |

## 逐项

| # | ID | 复现（整改前） | 根因 | 修复 | 回归测试 |
|---|---|---|---|---|---|
| 1 | QA-001 | `echo safe⏎touch x` 在 plan + `on_ask=deny` 下执行；`echo safe⏎rm -rf subdir` 判为 read | shlex 把换行当空白 | `_bash_lines` 按 bash 规则改写命令边界：引号外换行 → `;`，续行，词首 `#` 注释，here-document 正文（找不到结束符时按命令分析），`$(( ))` 里的 `<<` 不当 here-document | `test_command_boundaries_follow_bash`、`..._do_not_overreach`、`test_newline_cannot_smuggle_a_write_into_plan_mode` |
| 2 | QA-002 | default 下 `read .SSH/config`、`cat .SSH/config`、`grep` 根 `.SSH` 都读出 canary | 清单与线索按原样大小写匹配，APFS 不区分大小写 | `sensitivity()` 与 `CREDENTIAL_HINTS`/`SECRET_HINTS` 不区分大小写；`deny`/`ask` 规则不区分大小写，`allow` 仍按原样，不因此变宽 | 凭证样例追加 `.SSH/config` 等五个别名；`test_case_alias_of_credentials_never_reaches_the_model` 在真实文件上读一遍 |
| 3 | QA-003 | `ps eww -p $PPID` 自动放行并打印父进程密钥；`MYSQL_PWD` 留在子进程 | `ps` 在 `READ_CMDS`；剥离只认 KEY/TOKEN 等字样 | `ps` 带 `-E` 或 BSD 风格 `e` 归入凭证档（与 `/proc/*/environ` 同档，任何模式拒绝），`ps -ef`/`ps aux` 不受影响（本机实测 `-e` 不打印环境）；剥离追加 `_PWD`/`_PASS`/`_PAT`/`_DSN` 结尾、PASSPHRASE、COOKIE，以及值为带口令 URL 的变量；`PWD`/`OLDPWD` 保留 | `test_bash_credential_tricks_...` 追加 `ps` 三种写法；`test_bash_credential_deny_stays_narrow` 追加 `ps -ef` 等；`test_secret_like_env_names_are_stripped_from_bash` |
| 4 | QA-004 | `Origin: http://127.0.0.1:9999` 建会话 200，带令牌也是 200 | 只比较主机名 | `_same_origin`：Origin 与本次请求的 Host 主机名、端口都相同；`null` 与非 http(s) 一律不算 | `test_server_rejects_writes_from_other_local_ports`（有无令牌两种）；真实服务与浏览器跨端口复核见 fix-uat.json |
| 5 | QA-005 | `bash -lc 'rm -rf ~'`、`find ~ -delete` 不是灾难性，bypass 放行；记住一次 `bash -lc` 后另一段 `bash -lc` 不再询问 | `-c` 要求整词；`find` 不看起点；授权取前两词加 `*` | 识别合写短选项（`-lc`、`-ec`）与 `-o` 带值；`find -delete` 起点是根、家目录或其一级目录时灾难性，`-exec` 的命令把 `{}` 换成起点递归分析；命令里嵌代码（`sh -c`、`eval`、`find -exec`、解释器内联、命令替换）、包装命令或前置赋值时，会话授权只精确匹配这一条 | 参数化灾难性用例；`test_session_grant_for_inline_script_is_exact`；`test_session_grant_shapes`（含换行命令的授权能被 `Rule.parse` 读回） |
| 6 | QA-006 | 假 `DEEPSEEK_API_KEY` 下 `resolve("mock")` 得到 deepseek；示例 11 失败 | `mock` 是供应商名，被当成模型名交给 auto 探测 | 只写供应商名等同带前缀；演示模型名（`mock`、`flywhl-demo`、`scripted`）只走演示供应商，provider 配置与探测改不了；示例 11 子进程环境改为白名单 | `test_model_spec_resolution` 追加四条；示例 11 带假密钥、代理指向死端口复跑 |
| 7 | QA-009 | `harness.db`、`-wal`、`-shm` 为 0644 | 沿用 umask | 库文件先以 0600 建好再交给 SQLite（-wal/-shm 沿用主库权限），已有的 0644 文件打开时收紧；新建数据目录 0700 | `test_session_db_is_private_to_owner` |
| 8 | QA-010 | 375px 下打开检查器后，覆盖层盖住页头的开关，没有遮罩和关闭入口，关不掉 | 覆盖层 `top: 0` | 覆盖层从页头下方开始；加遮罩，点击收起；窄屏 Esc 收起；收起不改写桌面偏好 | 浏览器验收（375×812 与 1440×900），未写成 pytest |
| 9 | QA-011 | bypass 下 `cat /etc/master.passwd`、`security find-generic-password -w` 放行 | 清单只有 `/etc/shadow`；钥匙串查询在线索档 | `*/etc/master.passwd` 进凭证清单；`find-generic-password`、`find-internet-password`、`dump-keychain` 进凭证线索，任何模式拒绝 | 凭证样例与 bypass 用例 |
| 10 | QA-012 | `/api/info?token=…` 在任意路径有效 | 中间件对所有路径读查询串 | 查询串令牌只在 `/` 兑换（随后 303 去掉），其他路径只认 cookie 与 `Authorization` | `test_query_token_is_only_exchanged_at_root` |

## 核查中发现的同根因变体

下面几条不在审计台账里，和 QA-001、QA-005 是同一个根因（分析器对命令边界的理解与 bash 不一致），已在同一批修复。第一条比 QA-001 更严重：

| 命令 | 整改前 | 原因 |
|---|---|---|
| `echo a#b; rm -rf ~` | **default 模式直接放行**（判为只读 echo） | shlex 把词中间的 `#` 当注释，后半句整个消失 |
| `echo hi # c⏎rm -rf ~` | 放行 | 注释吞掉换行，下一行并进 echo 的参数 |
| `if true; then rm -rf ~; fi`、`{ rm -rf ~; }`、`! rm -rf ~` | bypass 放行 | 保留字被当成命令名 |
| `ls;(rm -rf ~)`、`echo x \|& rm -rf ~` | bypass 放行 | `;(` 被 shlex 并成一个 token；`\|&` 不在分隔符里 |
| `find ~ -exec rm -rf {} +` | bypass 放行 | `-exec` 的命令没有分析 |

## 新发现

- **`.env` 覆盖命令行的 `--provider mock`。** CLI 加载启动目录的 `.env`，其中带前缀的 `LLM_PRIMARY_MODEL=deepseek:…` 优先级高于 `--provider mock`，演示服务会改走真实供应商。本次验收第一次在仓库根目录启动服务时踩到，产生 1 次真实调用（约 2.5K 输入 / 101 输出 token，估算 $0.0009），发现后立即停掉并改为在临时目录启动、显式 `--model mock`。
  - 后续已修复：`HarnessConfig.load` 的显式参数只给 `provider`（不是 auto）时，文件、环境变量、`.env` 留下的模型若自带别家供应商（前缀、只写供应商名、演示模型名）就丢掉，改用该供应商的默认模型；不带供应商的模型名照常保留。回归测试见 `tests/test_providers.py` 的 `test_explicit_provider_beats_model_pinned_by_lower_layers` 与 `test_cli_provider_beats_prefixed_model_from_dotenv`。

## 残余风险

- 命令分析仍然不是安全边界。`_bash_lines` 是对 bash 词法的近似，`case` 分支的 `)`、`${…}` 里的引号这类少见写法可能判错；判错时多数是多拆（多问一次），真正的限制仍要靠沙箱（QA-008）。
- 大小写不敏感匹配在区分大小写的 Linux 上会多拒名字只差大小写的文件，属于硬拒绝，没有批准后执行的出路；这类文件极少。
- `ps` 之外读取进程环境的途径（`sysctl kern.procargs2`、调试器）不在只读清单里，default 下要确认，bypass 下放行。
- live 33 项、覆盖率、依赖漏洞扫描仍未做，与审计时相同。

## 提交批次

| 批次 | 内容 |
|---|---|
| 1 | 命令分析器：命令边界与 bash 一致，`-lc`、`find -delete/-exec`，`ps` 读进程环境归入凭证档，会话授权精确化（QA-001、QA-003、QA-005） |
| 2 | 凭证与密钥：大小写别名、子进程密钥剥离、`master.passwd` 与钥匙串（QA-002、QA-003、QA-011） |
| 3 | HTTP：Origin 比较端口，查询串令牌只在 `/` 兑换（QA-004、QA-012） |
| 4 | 演示模型解析与示例 11 环境白名单（QA-006） |
| 5 | 会话库与数据目录权限（QA-009） |
| 6 | 窄屏检查器覆盖层（QA-010） |
| 7 | 审计报告、整改记录与证据 |

每个批次的提交状态都单独跑过全量离线测试（308 → 318 → 321 → 321 → 322 → 322）。
