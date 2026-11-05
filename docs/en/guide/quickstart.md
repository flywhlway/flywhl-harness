# 02 Quick start

This page gets flywhl-harness running and tours the Web UI. Most mechanisms in later chapters can be watched live in these panels.

The language of the interface matters up front. The console, the CLI messages and the scripted demo model are all in Chinese. A real model replies in the language you write to it in, so an English prompt gets an English answer. The demo model is the exception: it matches the exact Chinese phrases in the table below.

## Install

You need Python 3.11 or newer, on macOS or Linux.

```bash
pipx install flywhl-harness      # or: uv tool install flywhl-harness
```

On Linux you also need [bubblewrap](https://github.com/containers/bubblewrap) (`sudo apt install bubblewrap`). The bash tool runs inside an OS sandbox by default and refuses to run without one; it never falls back to running unsandboxed. See [18 Workspace and sandbox](/en/governance/workspace-sandbox).

The `anthropic` extra is only needed for the Anthropic provider: `pipx install 'flywhl-harness[anthropic]'`. Besides the standard library, the package depends on `httpx`, `fastapi` and `uvicorn`.

To read the code and run the examples, clone the repo instead:

```bash
git clone https://github.com/flywhlway/flywhl-harness && cd flywhl-harness
uv sync --all-extras             # or: python -m venv .venv && .venv/bin/pip install -e ".[dev,anthropic]"
```

In the commands below, `playground` is the demo workspace inside the repo. With a pip install, use any empty directory.

## Start the Web UI

```bash
mkdir agent-demo
flywhl serve -w agent-demo       # in a clone: .venv/bin/flywhl serve -w playground
```

Open the link with the token that the terminal prints (`http://127.0.0.1:8765/?token=...`). After that, the same browser can go straight to `http://127.0.0.1:8765`. The token is on by default and stored in `~/.flywhl/server_token`, so it survives restarts.

`-w` sets the workspace. All file operations by the agent are confined to that directory. Data lives in `.flywhl/` inside it: the session database, snapshots and the demo "external system".

With no API key set, the harness uses the built-in demo model, **flywhl-demo**. It is not smart. It follows fixed scripts and calls tools, which is enough to show every mechanism. The "demo scenarios" cards on the start page, and the template menu at the lower left of the input box, use these phrases. Type them exactly as written, in Chinese:

| Phrase | What you will see | Chapter |
|---|---|---|
| `列出目录` (list the directory) / `读取 README.md` (read README.md) / `搜索 def` (search for `def`) | Read-only tools auto-approved, line-numbered reads, truncation notice | [06](/en/core/tools) |
| `创建 hello.py` (create hello.py) | In default mode a write opens an **approval card**; the run enters `waiting_human`; a shadow-git snapshot is taken before the write | [17](/en/governance/permissions-hitl), [18](/en/governance/workspace-sandbox) |
| ``运行 `python3 hello.py` `` (run `python3 hello.py`) | Bash command risk analysis, streamed output | [17](/en/governance/permissions-hitl) |
| `并行检查` (parallel check) | Three read-only calls in one turn run **concurrently**; results come back in call order | [06](/en/core/tools) |
| `制定计划` (make a plan) | External plan via `todo_write`, plan recitation, Stop check | [07](/en/core/context-engineering), [08](/en/core/termination-budget) |
| `委托子任务` (delegate a subtask) | A subagent works in an isolated session and returns only a structured contract | [20](/en/multi-agent/subagents) |
| `等待 20 秒` (wait 20 seconds) | An interruptible tool; press Stop and watch `running → cancelling → cancelled` | [12](/en/reliability/cancellation) |
| `发送通知` (send a notification) | An `external` side effect with an idempotency key | [14](/en/reliability/idempotency) |
| `压缩演示` (compaction demo) | Context usage climbs, old results are cleared, then a summary compaction | [07](/en/core/context-engineering) |
| `循环测试` (loop test) | Repeated-call detection: a warning first, then the run ends as `loop_detected` | [08](/en/core/termination-budget) |
| `错误演示` (error demo) | A tool fails, the error goes back to the model as a result, the model changes approach | [06](/en/core/tools) |

### UI tour

```
+- Sidebar ----------+- Session header: title, run status, live link, permission mode, pause / stop / resume / retry -+- Inspector -+
| New session        |                                                                                              | Overview    |
| Search / palette   |  Conversation: messages, thinking, tool calls (click for arguments, live output, result,     | Context     |
| Runs               |  diff, snapshot), approval cards, injected reminders, compaction dividers, run summary       | Events      |
| Approvals (count)  |                                                                                              | History     |
| Tools              +- Input: "Send" when idle; switches to "steer / queue" while a run is active -----------------+             |
| Recent sessions    |                                                                                              |             |
+--------------------+----------------------------------------------------------------------------------------------+-------------+
```

The sidebar has three management pages. **Runs** lists the runs of all sessions, with status filters, search and sorting by tokens, cost or duration. **Approvals** is a cross-session inbox where you can approve, edit the arguments and approve, or reject. **Tools** lists every tool with its effect level. `Cmd+K` (`Ctrl+K` elsewhere) opens the command palette to search sessions and switch permission mode or theme. `/` focuses the input and `Cmd+\` shows or hides the inspector.

The inspector on the right is the most useful part for learning:

- **Overview**: the current run's ID, attempt, version, worker and heartbeat; budget bars (turns, tool calls, tokens, cost); every CAS state transition with its reason and initiator (runs that ended before the page opened are loaded from the trace); an expandable full state machine; the session's earlier runs (resume or retry, deterministic replay, trace JSON download).
- **Context**: current usage against the two threshold lines (clear old results, compact to a summary); the token split between system, tools and messages; each message the model sees now with its share of tokens; the active compaction summary; the frozen system prompt.
- **Events**: the raw event stream with `seq`, filterable by type and searchable. "Simulate reconnect" drops the SSE connection on purpose and resumes two seconds later with `Last-Event-ID`. Check that `seq` stays continuous with no repeats.
- **History**: the append-only session branch tree. "Branch from here" moves `head` to any earlier point, and your next input grows a new branch. Below it is the list of workspace snapshots, any of which you can roll back to (after a confirmation).

## Connect a real model

Models are written as `<provider>:<model>`. Put keys in environment variables or in a `.env` file in the launch directory. On startup `flywhl` reads provider keys and model choices from `.env` without overriding variables already set. Variables for addresses, proxies and permission mode are ignored with a notice; set those in the real environment or pass `--env-file` explicitly. The reason is in [19 Security](/en/governance/security).

```bash
cp .env.example .env        # fill in the keys you need; .env is already in .gitignore
flywhl doctor               # connectivity check: one streamed text call and one tool round trip per configured provider
flywhl serve -w playground  # with DEEPSEEK_API_KEY set, auto mode picks deepseek:deepseek-flash
```

`auto` mode picks the first provider that has a key, in this order: DeepSeek, Anthropic, OpenAI, DashScope (Alibaba Bailian), Kimi, Zhipu, MiniMax, OpenRouter. With none, it uses the built-in demo model. You can also name one explicitly:

```bash
export DEEPSEEK_API_KEY=sk-...
flywhl serve -w playground                                   # same as --model deepseek:deepseek-flash
flywhl serve -w playground --model deepseek:deepseek-v4-pro

export DASHSCOPE_API_KEY=sk-...                              # Alibaba Bailian
flywhl serve -w playground --model dashscope:qwen3.8-flash
export KIMI_API_KEY=sk-...                                   # Moonshot
flywhl serve -w playground --model kimi:kimi-k2.6
export MINIMAX_API_KEY=...                                   # MiniMax puts thinking inline in <think>; the harness splits it out
flywhl serve -w playground --model minimax:MiniMax-M2.7

export ANTHROPIC_API_KEY=sk-ant-...
flywhl serve -w playground --model anthropic:claude-opus-5-5

flywhl chat -w playground --model ollama:qwen3.5:9b-mlx      # local Ollama, default http://localhost:11434/v1, override with OLLAMA_BASE_URL

export OPENAI_API_KEY=sk-...                                 # any OpenAI-compatible service
export OPENAI_BASE_URL=https://your-gateway.example.com/v1
flywhl serve -w playground --model openai:your-model
```

`flywhl doctor` shows which layer is broken. Each provider gets a line with the result of the streaming text check and the tool round trip, with timings. A failure prints an error class in brackets, for example:

```text
x zhipu:glm-5.3-flash  Zhipu (GLM)
    [billing HTTP 429] {"error":{"code":"1113", ...}}
```

With no arguments it checks the configured primary model and fallback chain (marking the position), plus the default model of every provider that has a key. A dead entry in the fallback chain is flagged separately, since finding out only when you fall back to it is too late. The error class decides what the harness does: `not_found` and `billing` move along the fallback chain to another model or provider and are not retried in place; `rate_limit` is retried with backoff. To check one model, run `flywhl doctor <provider>:<model>`.

**Thinking models.** `thinking = "enabled" | "disabled"` and `reasoning_effort` are translated into each provider's own fields (DeepSeek's `thinking.type`, Bailian's `enable_thinking`, OpenRouter's `reasoning`) and ignored where unsupported. DeepSeek V4 thinks by default.

**Two defaults for Anthropic models.**

- Prefix caching: requests carry a top-level `cache_control`, and the system prompt and tool set are frozen within a session, so a repeated prefix is billed at the cache rate.
- Server-side fallback: for supported models the request sends `fallbacks: "default"` (beta `server-side-fallback-2026-07-01`), so when a safety policy refuses a request the server retries with a recommended fallback model. The parameter goes only to the official endpoint. It is turned off when Claude is reached through a compatible gateway such as OpenRouter, where gateways were seen to answer it with a 400.

Details are in [05 Messages and model access](/en/core/messages-and-models).

## Command line

```bash
flywhl chat -w playground               # terminal chat; Ctrl-C cancels the current run, press twice to quit
flywhl run "summarize README" --json    # one-shot run; stdout is a JSONL event stream
flywhl run "fix the TODOs and run tests" --yes   # auto-approve actions that need approval (trusted environments only)
flywhl runs                             # list runs: status, attempt, version, tokens
flywhl resume <run_id>                  # resume a paused or interrupted run
flywhl retry <run_id>                   # retry a failed run from its history, attempt + 1
flywhl recover                          # take over runs whose heartbeat expired (crashed process) and resume them
flywhl replay <run_id>                  # deterministic replay: no model calls, no side effects
flywhl replay <run_id> --fork-at 2      # first 2 model calls from the recording, then the current model
flywhl trace <run_id> -o trace.json     # export the trace (with OTel GenAI style spans)
flywhl tools                            # tools and their effect levels
flywhl doctor                           # model connectivity check
```

Two more subcommands exist: `flywhl sessions` lists sessions, and `flywhl export <session_id>` writes a session as a Markdown transcript.

In `--json` mode the harness runs unattended: actions that need approval are **denied** by default, and `--yes` allows them instead. Blocking in CI on an approval that never arrives is worse than failing fast.

## Configuration file

Put a `flywhl.toml` in the launch directory (which is also the default workspace). Every field has a default, and unknown fields are an error, so a typo is never silently ignored.

```toml
model = "deepseek:deepseek-flash"
fallback_models = ["dashscope:qwen3.8-flash"]   # fallback chain, may cross providers
max_tokens = 8192
verify_command = "python3 -m pytest -q"      # Stop check: the run can't end while tests fail

[budget]
max_turns = 60
max_cost_usd = 0.5
max_wall_seconds = 1800

[context]
compact_threshold = 0.8
keep_recent_tokens = 12000
tool_result_max_tokens = 6000

[permissions]
mode = "default"                             # default | accept_edits | plan | bypass
rules = ["allow:bash(git status*)", "allow:bash(python3 -m pytest*)", "deny:bash(git push*)"]

[[hooks]]                                    # out-of-process hook, Claude Code compatible protocol
event = "pre_tool_use"
matcher = "bash"
command = "python3 scripts/guard.py"
fail_closed = true

[mcp_servers.demo]                           # MCP server: tool names get an mcp__demo__ prefix
command = "python3"
args = ["examples/mcp_echo_server.py"]
```

`verify_command`, allow rules, `[[hooks]]` and `[mcp_servers]` run commands or loosen approval. When they come from the `flywhl.toml` in the launch directory, you must trust that file first: the terminal asks item by item, or pass `--trust`. The reason is in [19 Security](/en/governance/security).

The workspace needs trusting once too: the terminal asks on the first run in a repository, `--trust` records it, and `--json` or piped runs neither ask nor trust. In an untrusted workspace, what the agent reads counts as untrusted content and later writes need a person, so a CI run on your own code should pass `--trust`.

Load order: defaults, then the user config `~/.flywhl/config.toml`, then `flywhl.toml`, then environment variables (`FLYWHL_PROVIDER`, `FLYWHL_MODEL`, `FLYWHL_BASE_URL`, `FLYWHL_PERMISSION_MODE`, and others), then command-line arguments. The full field list is in `config.py`.

## Use it as a library

```python
import asyncio
from flywhl_harness import Harness, HarnessConfig

async def main():
    async with Harness(HarnessConfig.load(workspace="playground")) as h:
        result = await h.run("list the directory", on_event=lambda ev: print(ev.seq, ev.type))
        print(result.status, result.text)

asyncio.run(main())
```

`Harness` is the facade: `create_session`, `start_run`, `subscribe`, `cancel`, `pause`, `resume`, `retry`, `steer`, `resolve_approval`, `recover`. On start, `flywhl serve` takes over runs left behind by a crash, then checks again periodically. When the service stops, running runs **pause** at the next safe point and continue on the next start. Every consumer (CLI, Web, your code) depends only on the event stream, which is the first design principle in [03 Architecture overview](/en/guide/architecture).

## Tests and examples

In a clone:

```bash
.venv/bin/python -m pytest -q                       # offline tests (excludes tests/live)
.venv/bin/python examples/02_crash_resume.py        # real kill -9, then recovery with the side effect happening exactly once
.venv/bin/python examples/11_kill9_cli_recover.py   # same, with two CLI processes, printing the state transition trace
```

The 11 numbered scripts in `examples/` all use scripted or built-in demo models and need no API key. The comment at the top of each says what it demonstrates and what to watch.

## Where the data lives

```
playground/.flywhl/
+-- harness.db            # SQLite (WAL): sessions / entries / runs / steps / events / approvals / blobs
+-- external_notify.db    # the demo "external system": send_notification writes here, deduplicated by idempotency key
+-- shadow.git/           # shadow git repo: workspace snapshots before writes, separate from your own .git
```

To start over, delete `.flywhl/`.

## Troubleshooting

**Port in use?** `flywhl serve --port 9000`.

**Need access from the LAN or behind a reverse proxy?** `flywhl serve --host 0.0.0.0` generates an access token automatically and prints a link with `?token=...`; open it once. For a custom domain, add it to the Host allowlist with `--allow-host <domain>`. By default the server accepts only `127.0.0.1` and `localhost` as Host and rejects cross-site writes. It can approve actions and switch permission mode, which amounts to running commands on the machine. See [19 Security](/en/governance/security).

**Does the Web UI need the internet?** No. It uses system fonts and built-in icons and requests no external resources.

**A real model won't connect or won't call tools?** Run `flywhl doctor <provider>:<model>` first. It reports auth, streaming and the tool round trip separately, with the error class (`auth`, `billing`, `not_found`, and so on). Small local models call tools poorly, so a result there is a lower bound.

**No approval card, and the command just ran?** Check whether the permission mode in the session header is full-allow (`bypass`), and whether `flywhl.toml` has a matching `allow:` rule.

Next: [03 Architecture overview](/en/guide/architecture). [Read the full chapter in Chinese](/guide/quickstart).
