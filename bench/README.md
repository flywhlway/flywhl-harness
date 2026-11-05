# 基准

方案和不下的结论见 [docs/benchmark/methodology.md](../docs/benchmark/methodology.md)，这里只讲怎么跑。

真实模型会在机器上执行命令，所以一律在一次性容器里跑，运行器在宿主机上直接拒绝。不挂任何宿主目录，原始数据不出容器。

## 目录

| 路径 | 内容 |
|---|---|
| `plan.toml` | 机器可读的方案：模型、次数、预算、配置 |
| `tasks/<id>/` | 30 个任务，格式见 `taskset.py` 开头 |
| `q1.py`、`q2.py`、`q3.py` | 三个问题的运行器 |
| `redact.py` | 脱敏与发布前扫描 |
| `report.py` | 从结果算数字，写站点页面 |
| `rehearsal.py` | 彩排用的脚本化模型 |
| `Dockerfile`、`run.sh` | 一次性环境 |

## 步骤

先彩排，不花钱，确认流水线是通的。脚本模型做题时，A 配置下 5 个沙箱敏感任务应当失败，其余都通过，回放没有分叉，脱敏没有命中。不符合就退出码 1：

```bash
bench/run.sh --out /tmp/bench-rehearsal rehearse
```

把被测模型的密钥写进一个 `--env-file` 格式的文件，别的变量不要放。这个文件不进仓库。最好是设了额度的专用密钥。然后每家跑一遍，量出单价：

```bash
bench/run.sh --env-file ~/bench.env --out /tmp/bench-pilot pilot
bench/run.sh --env-file ~/bench.env --out /tmp/bench-pilot-q2 pilot-q2
```

`pilot-q2` 用方案里的 4 个任务走通 Q2，数据不计入。

确认次数放得下预算，再正式运行：

```bash
bench/run.sh --env-file ~/bench.env --out /tmp/bench-results all
```

结果绑定当前提交。版本已经发到 PyPI 时，加上 `--spec 'flywhl-harness==<版本>'`，别人能装到同一份代码。

中途停了（预算到了、断网）时容器会留着，同样的命令接着跑，跑完的试验不会重复花钱。

## 归类和出报告

结果里每个失败都要归类，写在结果目录的 `classifications.jsonl`，一行一条，分类的取值见方法说明：

```json
{"id": "q2/env-git-hook/A/1", "category": "sandbox_boundary", "note": ".git/hooks 只读，写入被拒绝"}
```

`report` 不调用模型，可以在宿主机上运行：

```bash
.venv/bin/python -m bench report --out /tmp/bench-results --draft
.venv/bin/python -m bench report --out /tmp/bench-results --write docs
```

`--draft` 列出未归类的失败。`--write` 把数字写进站点页面，有未归类的失败或者数据来自彩排时拒绝写入。结论的措辞由人写，生成的页面只有数字。

运行偏离方案时，把经过写进结果目录的 `deviations.json`（`[{"zh": "...", "en": "..."}]`），页面会列出来。试验作废要保留原始记录：挪到 `q2/voided.jsonl`，它随数据发布，不进统计。

`classifications.jsonl` 和 `deviations.json` 只在宿主上写：容器里的同名文件不会带出来，模型在无沙箱配置下写得到结果目录。两者的内容会公开，写入页面前过一遍和发布时相同的脱敏检查，查到疑似密钥或路径就拒绝。

## 改任务

改完经 `scripts/safe_pytest.py` 校验，检查脚本和参考答案都是会执行的代码，不要直接在宿主机上跑：

```bash
.venv/bin/python scripts/safe_pytest.py -q tests/test_bench.py
```

运行开始之后不改任务。要改就在 `plan.toml` 换一个 `plan_id`。
