# 全历史密钥扫描（2026-11-06）

## 结论

gitleaks 在全部历史上 **0 条命中**。独立的正则交叉校验有 4 处命中，逐条核对都是测试假值。没有发现真实密钥，没有修改历史。

这次没有用 `gitleaks git` 子命令，原因和替代做法见“方法”。

## 工具

| 项 | 值 |
|---|---|
| 工具 | gitleaks v8.30.1，默认规则 |
| 镜像 | `zricethezav/gitleaks@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f`，来自 Docker Hub |
| 运行方式 | 一次性容器，`--network none`，只读挂载 |
| 参数 | `dir /scan/history --redact --no-banner --no-color --report-format json --report-path -` |

## 范围

引用来自 2026-11-06 的 `git clone --mirror`，`main` 在 `8bd4b26`。

| 项 | 数量 |
|---|---|
| 分支 | 9 |
| PR 引用 | 11，其中 1 个是开放 PR 的合并预览 |
| 标签 | 4，都是带注释的标签 |
| 提交 | 127，其中合并提交 10 |
| 带新增内容的文件版本 | 1055 |
| 提交信息 | 127 条 |
| 标签说明 | 4 条 |
| gitleaks 实际扫描 | 约 4.12 MB |

## 方法

这次扫描由 worktree 隔离的自动化代理执行，隔离检查不允许 `docker run` 的参数里出现 `git`，所以没有按 `scripts/secret_scan.sh` 的方式跑。改成：

1. 从镜像的 `packed-refs` 取出全部引用的 tip，范围和 `--all` 相同。
2. 在本地仓库用 `git log -p -U0 --full-history --diff-filter=tuxdb` 导出新增内容，这是 gitleaks `git` 模式取历史的方式。1 个本地没有的 PR 合并预览按 SHA 取回。
3. 按 `<提交>/<原路径>` 写成文件，保留真实行号，让路径白名单和 `git` 模式一样生效。
4. 用 gitleaks `dir` 扫描这棵目录树，容器里不运行 git。

比 `git` 模式多扫了提交信息和标签说明。`scripts/secret_scan.sh` 用的是 `git` 子命令，这次只过了 `shellcheck`，**没有实跑**。

## 验证

**阳性对照。** 随机生成的假令牌和假私钥走同一条流程，放在源码、点目录和提交信息三个位置，3 条全部报出，行号正确。假值只存在于临时目录。

**交叉校验。** gitleaks 默认带路径白名单，实际扫描量也小于目录树（4.12 MB 对 4.25 MB）。所以用 11 条独立的正则把 1186 个文件不分路径再查一遍，覆盖 GitHub、Anthropic、OpenAI 风格、PyPI、AWS、Slack、Google 的令牌格式，以及私钥块、URL 里的账号密码、Bearer 字面量和高熵赋值。

| 规则（本次自写） | 文件 | 提交 | 结论 |
|---|---|---|---|
| `openai-style-key` | `tests/test_bench.py` | `ea23de5` | 测试假值。脱敏用例的输入，只有小写字母和连字符，不符合真实密钥的形状 |
| `basic-auth-url` | `tests/test_backlog.py` | `486d169` | 测试假值。主机是 `git.example.com` |
| `basic-auth-url` | `tests/test_security.py` | `4c85703` | 测试假值。环境变量用例里的连接串，主机是 `db` |
| `assign-high-entropy` | `docs/quality-audit/tests/test_security_invariants.py` | `8adf2be` | 测试假值。值里写着 `canary`，不符合真实密钥的形状 |

**敏感文件名。** 历史里只有 `.env.example` 的 2 个版本，密钥项的值都是空的。没有 `.env`、私钥、证书或 `server_token`。

**合并提交。** `git log --merges --cc` 没有输出任何 diff，10 个合并提交没有独有内容。

## 没有覆盖到的

| 对象 | 处理 |
|---|---|
| 二进制文件的 5 个版本 | `bench/tasks/env-venv-wheel/workspace/wheels/hello_bench-1.0.0-py3-none-any.whl` 解压后单独扫描，0 条命中。直接扫这个文件时 gitleaks 不进 `.whl`，报 0 字节，所以不能只开 `--max-archive-depth`。`docs/quality-audit/evidence/` 下的 4 张截图逐张看过，没有地址栏、令牌或密钥，没有做 OCR |
| 文件类型变更 | `--diff-filter=tuxdb` 不看，和 `git` 模式一致 |
| 仓库之外 | Issue、PR 评论、Actions 日志、Release 附件没有扫。基准数据包发布前由 `bench/redact.py` 扫描，这里没有复核 |
| 本机没有推送的分支 | 不在镜像里，没有扫 |
| 规则认不出的格式 | 默认规则和上面的正则都按已知格式匹配，自定义格式的密钥可能漏掉 |

GitHub 侧的 secret scanning 和 push protection 处于开启状态（2026-11-06 经 API 只读查询）。
