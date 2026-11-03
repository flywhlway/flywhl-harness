# 第二轮安全加固记录

- **日期：** 2026-10-31。
- **基线：** `main` @ `924ab1d`。
- **方法：** 每项先用探针复现，再写回归测试，最后改实现。探针只调用纯判定函数，或只在临时目录里执行。
- **修复前的数据来源：** 探针结果，以及在旧代码上做的纯判定对比。
- **事故：** 曾在宿主机上用旧代码跑执行型回归测试，引发了一次事故，见 [incident-2026-10-31.md](incident-2026-10-31.md)。

## 结论

| 指标 | 修复前 | 修复后 |
|---|---|---|
| 离线套件 `pytest -q` | 324 passed | **455 passed**（另有 33 个真实模型用例默认不选） |
| 本轮回归 `tests/test_hardening.py` | — | **119 passed**（36 个用例函数，展开参数后 119 项） |
| 审计不变量套件 `docs/quality-audit/tests` | 13 passed | 13 passed |

第一轮遗留问题的处理：
- **QA-007** 已关闭：`pre_tool_use` 的命令 hook 默认 fail-closed。
- **QA-013** 已关闭：README 里的测试计数已更新。
- **QA-008** 部分处理：沙箱现在可以断网，但仍然默认关闭，也不限制钥匙串查询（Mach 服务）。

## 修复项

### 命令分析：伪装成只读的执行

修复前：带执行或写入选项的命令、被环境变量劫持的命令，都按只读放行。在 plan 模式、`on_ask=deny` 下，它们会零审批执行。

现在判为 external 或 write，需要审批。例如：
- `rg --pre=./x.sh`
- `git -c core.fsmonitor=./x status`
- `git --exec-path=. status`
- `PATH=. ; ls`
- `LD_PRELOAD=./x.so cat a.txt`
- `HOME=. git status`

用例：`test_read_commands_with_exec_or_write_options`、`test_environment_hijack_is_not_read_only`、`test_git_options_that_execute_or_write`、`test_plan_mode_cannot_run_code_disguised_as_reads`

### 命令分析：仓库配置让 git 执行外部程序

修复前，仓库 `.git/config` 里配置的外部程序会被只读的 git 命令执行：
- `core.fsmonitor` 和 post-index-change hook，会被 `git status` 执行；
- filter 和 textconv，会被 `git diff`、`git log -p`、`git show` 执行；在 `allow:bash(git diff*)` 这类规则下，完全不经审批。

现在：
- 仓库配置定义了外部程序时，这些 git 读命令一律要审批，前缀 allow 规则也不放行；
- bash 环境只认显式指定的裸仓库；
- worktree 会顺着 gitdir 文件找到真实的配置；
- 写入 `.git/` 在任何模式下都要审批，accept_edits 也不例外。

用例：`test_read_only_git_does_not_run_repository_configured_commands`、`test_git_reads_follow_repository_config`、`test_repository_diff_drivers_do_not_run_without_approval`、`test_bash_env_refuses_implicit_bare_repositories`、`test_git_worktree_config_is_found_through_gitdir_file`、`test_writes_into_git_dir_need_approval_in_every_mode`、`test_existing_git_config_env_is_preserved`

### 命令分析：边角情况

修复前：
- 一对空的反引号（Markdown 代码围栏就是）会让分析器抛出 StopIteration，整个 Run 被终止；
- `cat list.txt | xargs cat` 按只读放行。

现在：
- 分析器对任意输入都不抛异常，有随机模糊测试覆盖；
- `xargs` 读哪些文件由数据决定，不按只读放行；
- 工作区里的符号链接按链接目标判定；
- 包在 `timeout` 或 `sudo env … nice` 里的灾难命令，在 bypass 模式下也会被拒绝。

用例：`test_analyzer_never_raises_on_odd_input`、`test_xargs_paths_are_not_statically_known`、`test_symlinks_in_workspace_are_judged_by_their_target`、`test_wrapped_catastrophes_are_denied_even_in_bypass`

### 凭证清单

修复前：下面这些凭证位置不在清单里。

本轮新增：
- `.config/git/credentials`
- `.cargo/credentials*`、`.gem/credentials`
- `.terraform.d/credentials.tfrc.json`
- `.oci/`、`.s3cfg`、`.boto`、`rclone.conf`
- `.password-store/`、`Library/Keychains/`、`.local/share/keyrings/`
- `.flywhl/server_token`（拿到它就能替用户批准一切）

现在：文件工具、bash 分析和沙箱规则共用这一份清单。

用例：`test_every_sensitive_glob_is_exercised`

### 文件工具：判定之后换链接（TOCTOU）

修复前：权限判定通过之后，把链接换成指向凭证的位置，工具照样会读出来。

现在：执行时按真实路径再查一次凭证清单。

用例：`test_file_tools_recheck_credentials_at_execution`

### 正则搜索

修复前：灾难性回溯的正则直接在事件循环里执行，整个服务会跟着卡住，包括所有会话和 Web 控制台。

现在：
- 搜索放进子进程执行，并设了超时；
- 结果格式不变，敏感文件照常跳过。

用例：`test_catastrophic_regex_does_not_block_the_event_loop`、`test_regex_worker_results_match_inline_search`、`test_search_ref_timeout`

### Hook：审批之后的改写

修复前：审批人批准了改过的参数之后，hook 还能再把它改写成策略会拒绝的命令，改写后的版本不再经过策略判定。这正是事故的触发条件。

现在：改写之后会重新判定。

用例：`test_hook_rewrite_after_approval_is_rechecked`

### Hook：失败时的默认策略（QA-007）

修复前：命令 hook 崩溃或超时时，默认放行。

现在：
- `pre_tool_use` 默认失败即拒绝；
- 其余事件默认放行；
- 都可以用 `fail_closed` 覆盖。

用例：`test_command_hook_failure_policy_defaults_by_event`、`test_crashing_pre_tool_hook_blocks_by_default`

### macOS 沙箱（QA-008 部分）

修复前：
- 不能断网；
- 凭证规则区分大小写，磁盘上的 `Library/Keychains` 拦不住。

现在：
- `sandbox_network = "deny"` 禁止对外连接，包括回环地址上的 TCP；本机 Unix 套接字不受影响；
- 凭证规则不区分大小写；
- 不受信任的项目配置只能把网络收紧，不能放开。

用例：`test_sandbox_network_deny_blocks_outbound_connections`、`test_sandbox_credential_rules_ignore_case`、`test_untrusted_project_config_can_only_tighten_sandbox_network`

### Web：响应头与脚本

修复前：没有 CSP，页面里有内联脚本。

现在：
- 所有响应都带 CSP，错误响应也不例外。CSP 禁止内联脚本，并设了 `frame-ancestors 'none'`；
- 内联脚本移到了 `boot.js`。

用例：`test_server_sends_security_headers_on_every_response`、`test_console_has_no_inline_scripts`

### Web：访问令牌

修复前：
- 只有在非回环地址上监听时，才启用访问令牌；
- 令牌不会保存，服务重启后书签就失效了。

现在：
- 令牌默认开启，保存在 `~/.flywhl/server_token`，权限 0600，重启后继续使用同一个令牌。写入新令牌时先删掉旧文件再新建，旧文件过宽的权限不会沿用；
- `--no-token` 只能用于回环地址，在其他地址上监听时必须带令牌；
- Cookie 有效期 30 天。

用例：`test_server_token_is_persisted_owner_only_and_reused`、`test_no_token_is_refused_off_loopback`、`test_token_cookie_lasts_and_oversized_bodies_are_refused`

### Web：请求体

修复前：分块上传不带 Content-Length，能绕过 8 MB 的上限。

现在：拒绝分块上传，返回 411；超过 8 MB 返回 413。

用例：`test_token_cookie_lasts_and_oversized_bodies_are_refused`

### Web：快照回滚参数

修复前：
- 快照编号原样拼进 git 参数，传 `--output=<路径>` 能让 git 在任意位置写文件；
- git 报错时，接口返回 500。

现在：只接受十六进制的 SHA。

用例：`test_rollback_rejects_non_sha_arguments`

### Web：断线续传

修复前：`Last-Event-ID` 不是整数时，请求直接报错。

现在：回退为全量重放。

用例：`test_bad_last_event_id_falls_back_to_full_replay`

### 终端输出

修复前：模型或工具输出里的 ESC、OSC 序列能清屏、改写已经打印出来的审批提示，还能通过 OSC 52 写剪贴板。

现在：显示之前先剥离这些控制序列。

用例：`test_terminal_output_strips_control_sequences`

## 仍未覆盖

本轮结束时还有下面几项没覆盖，之后都在[能力清单](capability-backlog.md)的“第二批已实现”里补上了：

- **整条路径放在变量里：** `F=/etc/passwd; cat $F` 当时判为 read。现在命令里赋过值的变量按取值展开；值要到运行时才知道的（`F=$(…)`）仍只能按无法静态分析处理，见[第 19 章](../governance/security.md)。
- **沙箱：** 当时只有 macOS 原型，不限制 Mach 服务，没有出口白名单。现在有了 Linux 档、出口白名单（仅 macOS 档）和本机 IPC 限制，默认仍关闭。
- **其他通道：** 命令 hook 的子进程当时继承全部环境变量，MCP 工具注解里的 `readOnlyHint`、`idempotentHint` 会被直接采信。两项都已修复。

这些问题的优先级排序见 [capability-backlog.md](capability-backlog.md)。
