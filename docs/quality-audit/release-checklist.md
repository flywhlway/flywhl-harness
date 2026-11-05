# 发布检查清单

依据 [release.yml](../../.github/workflows/release.yml) 和 [0.2.0 门禁记录](evidence/release-gate-0.2.0.md)。把清单贴进发布 PR 逐项勾选，`X.Y.Z` 换成版本号，结果写进 `evidence/release-gate-X.Y.Z.md`。

流水线只查这几件事：离线测试、标签格式、包版本和标签一致、`twine check`、运行依赖漏洞、门禁记录文件存在、CHANGELOG 有带日期的一节、wheel 冒烟。其余都靠这份清单，live 和基准只有人跑。

## 哪些检查要重跑

门禁绑定候选提交。之后又有改动时：

| 候选提交之后改了 | 重跑 |
|---|---|
| 只有文档 | 不重跑 |
| `src/` | 全部，`pip-audit`、工作流静态检查、沙箱验收可以沿用 |
| `uv.lock`、`pyproject.toml`、`scripts/`、工作流，或 `src/flywhl_harness/` 下的 `policy/`、`tools/sandbox.py`、`tools/egress.py`、`tools/confine.py` | 全部 |

## 发布前门禁

### 候选版本

- [ ] **候选提交。** 在 `main` 上，`git status --short` 为空，记下 `git rev-parse --short HEAD`。
- [ ] **版本号。** 改 `src/flywhl_harness/__init__.py` 的 `__version__`。这是唯一来源，`pyproject.toml` 经 hatch 读取。预发布写成 `1.2.3rc1`。
- [ ] **CHANGELOG。** 有 `## [X.Y.Z] - YYYY-MM-DD` 一节且内容非空，破坏性变化写进“行为变化”。`grep -Eq '^## \[X\.Y\.Z\] - [0-9]{4}-[0-9]{2}-[0-9]{2}$' CHANGELOG.md` 通过。
- [ ] **SECURITY.md。** 次版本升级时更新“支持的版本”表。
- [ ] **CI。** `gh run list --workflow tests.yml --branch main --limit 3` 全绿。只改文档的提交不触发这个工作流，看 `src/` 相同的最近一次运行。

### 离线与沙箱

`FLYWHL_REQUIRE_SANDBOX=1` 要一直带着：不带时沙箱起不来，用例全部跳过，结果还是绿的。

在主检出里跑，不要在 git worktree 里跑。worktree 的 `.git` 指向主仓库，测试的外层沙箱读不到，`test_log_fixtures_survive_a_global_ignore_rule` 会失败。

- [ ] **离线套件，macOS，Python 3.11～3.13。**

  ```bash
  for py in 3.11 3.12 3.13; do
    uv sync --locked --all-extras --python "$py"
    FLYWHL_REQUIRE_SANDBOX=1 uv run pytest -q
  done
  ```

- [ ] **系统沙箱用例，macOS。** `FLYWHL_REQUIRE_SANDBOX=1 uv run pytest -q -m os_sandbox`，Linux 专用的用例会跳过。
- [ ] **离线套件和系统沙箱用例，Linux。** 以 CI 的 `pytest` 作业为准（Ubuntu，Python 3.11～3.13）。本机复现用一次性容器，参数照 `bench/run.sh`：加 `--init`，不挂宿主目录，放开 seccomp、apparmor 和 `/proc` 遮蔽，同时去掉全部 capability。`FLYWHL_TEST_REAL_SANDBOX=1` 只给 live 用，用到离线套件上会失败。
- [ ] **审计不变量，两个平台。** `FLYWHL_REQUIRE_SANDBOX=1 uv run pytest -q docs/quality-audit/tests`。macOS 13 passed，Linux 12 passed、1 skipped，Linux 看 CI。失败时不改断言。
- [ ] **沙箱验收，两个平台。** 看 CI 的 `sandbox-acceptance` 作业，或者手动跑。两边都要 23/23。虚拟环境放在家目录下，放在临时目录时脚本拒绝运行。

  ```bash
  uv build --wheel
  uv venv "$HOME/fresh" && uv pip install --python "$HOME/fresh/bin/python" dist/*.whl
  "$HOME/fresh/bin/python" scripts/sandbox_acceptance.py --internet --json sandbox-acceptance.json
  ```

- [ ] **覆盖率。** 看 CI 的 `coverage-audit` 作业：整体或关键模块比[基线](coverage-baseline.json)低超过 0.5 个百分点时作业失败，判定在 [`scripts/coverage_ratchet.py`](../../scripts/coverage_ratchet.py)。覆盖率涨了要提交新基线：从作业产物取 `coverage.json`，运行 `uv run python scripts/coverage_ratchet.py coverage.json --update`。往下调基线只能手改，理由写进 PR。

### 打包

- [ ] **构建和元数据。** `uv build`，然后 `uvx -q twine@7.0.0 check --strict dist/*`。twine 版本和流水线相同，6.2.0 不认 hatchling 1.33 生成的 Metadata 2.5。
- [ ] **wheel 冒烟。** 用上面验收的干净环境：`"$HOME/fresh/bin/python" -I scripts/smoke_install.py`。
- [ ] **sdist 解包后测试。** 在一次性 Linux 容器里解包到家目录，跑离线套件和系统沙箱用例。解到 `/tmp` 时外层 bubblewrap 用不了，会退回只隔离环境变量的模式。sdist 不带 `bench/`，`tests/test_bench.py` 整个跳过，跳过数比仓库多。

### 依赖

- [ ] **运行依赖漏洞。** 和 `release.yml` 相同，有已知漏洞就停。

  ```bash
  req="$(mktemp)"
  uv export --frozen --no-dev --extra anthropic --no-emit-project --format requirements-txt -o "$req"
  uvx -q pip-audit@2.10.1 -r "$req" --disable-pip --progress-spinner off
  ```

- [ ] **站点依赖。** `npm audit --prefix docs`，和门禁记录“已评估的例外”逐条比对。这些包不进 wheel 和 sdist，新增条目评估后写进记录。
- [ ] **工作流静态检查。** `actionlint` 没有告警。

### 真实模型与基准

这几项会花钱，在不挂宿主目录的一次性容器里跑，密钥文件只放被测模型的密钥，不进仓库。

- [ ] **33 个 live 场景。** 容器里用真实 bubblewrap，跑完核对日志里没有密钥。默认模型 deepseek-flash，约 3～5 分钟、0.03 美元。缺密钥的用例是跳过，不是通过，要看到 33 passed。失败按 [tests/live/README.md](../../tests/live/README.md) 分类。

  ```bash
  FLYWHL_TEST_SANDBOX=off FLYWHL_TEST_REAL_SANDBOX=1 python -m pytest -m live tests/live
  ```

- [ ] **回答语言。** 用真实模型各问一次：英文提问的回答没有汉字，中文提问回答中文。仓库里没有脚本，手动做。
- [ ] **基准回归。** 上次回归之后 `src/` 改过才做。上次的提交写在最新的 `evidence/bench-regress-*.md` 里，用 `git diff --stat <提交> HEAD -- src/` 判断。按修复影响的任务更新 `bench/regression.toml` 的 `id` 和 `tasks`，提交后运行，彩排要求回放分叉 0、脱敏命中 0，结果写进 `evidence/bench-regress-<日期>.md`。

  ```bash
  bench/run.sh --out <结果目录> rehearse
  bench/run.sh --env-file <密钥文件> --out <结果目录> regress
  ```

### 密钥与记录

- [ ] **全历史密钥扫描。** `scripts/secret_scan.sh <仓库之外的目录>`，退出码 0 才算没有命中。命中逐条判定，写进 `evidence/secret-scan-<日期>.md`，只写规则名、路径和提交短 SHA。疑似真实密钥不写进文件，停下发布，交给仓库所有者。上次结果见 [2026-11-06 的记录](evidence/secret-scan-2026-11-06.md)。
- [ ] **门禁记录。** `docs/quality-audit/evidence/release-gate-X.Y.Z.md` 写好并合入 `main`：候选提交、结果表、查出并修掉的问题、已评估的例外、没有闭环的事。文件不存在时 `build` 作业失败。

## 发布动作

- [ ] **确认发布配置。** `pypi` 要有 `required_reviewers`。PyPI、TestPyPI 的发布者填的是 `release.yml` 和对应的 environment 名字。

  ```bash
  gh api repos/flywhlway/flywhl-harness/environments --jq '.environments[] | {name, rules: [.protection_rules[].type]}'
  ```

- [ ] **打标签。** 标签打在已合入门禁记录的 `main` 上，写法只能是 `v1.2.3` 或带 `a`、`b`、`rc` 的预发布。任何 `v*` 标签都会触发发布，别的用途的标签不要以 `v` 开头。

  ```bash
  git switch main && git pull --ff-only
  git tag -a vX.Y.Z -m "vX.Y.Z"
  git push origin vX.Y.Z
  ```

- [ ] **跟踪流水线。** `gh run list --workflow release.yml --limit 1` 取运行编号，`gh run watch <运行编号>`。顺序是 `tests`、`build`，然后 `smoke`（Ubuntu、macOS 上的 Python 3.11～3.13），`testpypi` 自动发布，`pypi` 等批准，最后 `github-release`。
- [ ] **批准 `pypi`。** 由仓库所有者在运行页面批准，Agent 和脚本不代批。没人批准，这一步一直等着。PyPI 的版本发出去撤不回，批准前确认 `testpypi` 作业已通过。
- [ ] **作业失败时。** `gh run rerun <运行编号> --failed` 只重跑失败的作业，产物不重新构建，也不用重打标签。发布作业报 `invalid-publisher` 时先核对发布者配置，这个报错可能盖住真实原因。

## 发布后验证

- [ ] **PyPI 上有这个版本。** 用 JSON 接口判断。`pypi.org/project/...` 对脚本返回的 200 可能只是 Client Challenge 页面。

  ```bash
  curl -fsS https://pypi.org/pypi/flywhl-harness/X.Y.Z/json \
    | python3 -c 'import json, sys; [print(u["digests"]["sha256"], u["filename"]) for u in json.load(sys.stdin)["urls"]]'
  ```

- [ ] **文件摘要和流水线产物一致。** 和上一步的输出比对，TestPyPI 把域名换成 `test.pypi.org` 再比一次。

  ```bash
  gh run download <运行编号> -n dist -D <目录>
  shasum -a 256 <目录>/*
  ```

- [ ] **来源证明。** wheel 和 sdist 各查一次，返回 200。`curl -fsS -o /dev/null -w '%{http_code}\n' https://pypi.org/integrity/flywhl-harness/X.Y.Z/<文件名>/provenance`
- [ ] **从 PyPI 装到干净环境。** macOS 和一次性 Linux 容器，Python 3.11～3.13。版本号要对，不带令牌的请求返回 401。`python:3.x-slim` 没有 bubblewrap，这里只验证安装和启动，沙箱行为由沙箱验收覆盖。

  ```bash
  uv venv --python 3.12 "$HOME/pypi-check"
  uv pip install --python "$HOME/pypi-check/bin/python" "flywhl-harness==X.Y.Z"
  "$HOME/pypi-check/bin/flywhl" --version
  "$HOME/pypi-check/bin/flywhl" serve --port 8799 --token "$(openssl rand -hex 16)" -w <空目录> &
  curl -s -o /dev/null -w '%{http_code}\n' --retry 10 --retry-connrefused http://127.0.0.1:8799/api/sessions
  kill %1
  ```

- [ ] **PyPI 页面。** 上面 JSON 里的 `info.description` 是英文 README，`info.project_urls.Documentation` 指向 `/en/`。
- [ ] **GitHub Release。** 附件是 wheel 和 sdist，说明是 CHANGELOG 对应的一节，预发布的 `isPrerelease` 为 true。

  ```bash
  gh release view vX.Y.Z --json tagName,isPrerelease,assets --jq '{tagName, isPrerelease, assets: [.assets[].name]}'
  ```

- [ ] **站点。** 标签不触发部署，`__version__` 或 `docs/` 合入 `main` 才触发。`gh run list --workflow deploy-docs.yml --limit 1` 成功，页面标注新版本：`curl -fsS https://flywhlway.github.io/flywhl-harness/ | grep -c '对应 X.Y.Z'` 大于 0。
- [ ] **记下发布结果。** 门禁记录加“发布结果”一节：标签所在提交、运行链接、上面各项结果，走 PR 合入。
