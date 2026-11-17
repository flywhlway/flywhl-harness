# flywhl-harness

[![tests](https://github.com/flywhlway/flywhl-harness/actions/workflows/tests.yml/badge.svg)](https://github.com/flywhlway/flywhl-harness/actions/workflows/tests.yml)
[![PyPI](https://img.shields.io/pypi/v/flywhl-harness)](https://pypi.org/project/flywhl-harness/)

English | [简体中文](https://github.com/flywhlway/flywhl-harness/blob/main/README.md)

> **Agent = Model + Harness.** The model does the thinking; the harness does everything else.

flywhl-harness is an Agent Harness written by hand in Python, with no agent framework underneath: about 19,000 lines of Python that implement the
mechanisms a production harness needs as code you can run, test and replace. It comes with a web console and a 30-chapter tutorial.

It is meant for two kinds of reader: engineers who want to see how a harness works all the way down, and developers who want a local
coding agent that can open an untrusted repository without being handed the keys to the machine.

**The console, CLI messages, scripted demo and code comments are in Chinese.** The model replies in the language you write in. The
[English docs](https://flywhlway.github.io/flywhl-harness/en/) summarize every chapter and link to the Chinese full text.

## What it implements

| Area | What you get |
|---|---|
| Loop | Async-generator main loop with explicit phases; steering and queued follow-ups while a run is active; output cut off by the length limit is continued or redone in smaller steps, with the run's output limit raised as needed |
| Models | Unified messages and stream events; provider profiles for DeepSeek, Alibaba Bailian, Kimi, Zhipu, MiniMax, OpenRouter, Ollama and Anthropic; error classification, jittered retries, circuit breaking, cross-provider fallback |
| Tools | Three side-effect levels (read / write / external); concurrent reads, serialized writes, results returned in call order; oversized results moved behind handles; MCP client |
| Web | `web_search` / `web_fetch`, off by default; each session picks its level before it starts and can only tighten it; DeepSeek, Bocha and SearXNG with failover; fetch scope matched on full URLs (paging is the one relaxation) plus an exfiltration check; a single outbound HTTP path that allows public addresses only and pins the vetted IP; counts and cost go into the budget; optional vendor-side search for Anthropic models |
| Context | Context usage estimates, clearing of stale tool results, summary compaction that never splits a tool call from its result and always keeps the original task |
| State | Session / Entry / Run / Step / Event model on SQLite WAL, synced for power-loss durability by default (`durability = "full"`); branching session tree; append-only history |
| Lifecycle | 14-state run machine with compare-and-swap transitions; cooperative cancellation; pause and resume; heartbeat leases and crash takeover, so a `kill -9` still gives exactly-once external side effects |
| Policy | Four permission modes; deny > ask > allow rules; bash risk analysis; credential locations denied in every mode; workspace trust and taint tracking; a separate human gate for running outside the sandbox; hooks compatible with the Claude Code protocol; durable human approval with diff previews |
| Isolation | Default-on OS sandbox with an egress allowlist; path confinement; shadow-git snapshots and rollback; secrets stripped from child processes |
| Multi-agent | Subagents with isolated context and a structured return contract; DAG scheduling with concurrency quotas |
| Measurement | Trajectory export with OTel GenAI-style spans; deterministic replay and counterfactual forks; evaluation with Wilson intervals and McNemar tests |

## Quick start

Requires Python 3.11+ on macOS or Linux. Windows is not supported. On Linux, install
[bubblewrap](https://github.com/containers/bubblewrap) first (`sudo apt install bubblewrap`): bash runs in an OS sandbox by default and
refuses to run if there isn't one.

```bash
pipx install flywhl-harness      # or: uv tool install flywhl-harness

mkdir agent-demo
flywhl serve -w agent-demo       # open the tokenized link it prints
```

With no API key set, the console uses the built-in `flywhl-demo` model, a scripted model that needs no network. The demo cards on the start
page walk through approvals, cancellation, compaction, subagents and loop detection.

To use a real model (the default is `deepseek:deepseek-flash`):

```bash
cd agent-demo
export DEEPSEEK_API_KEY=sk-...
flywhl doctor                        # streaming and tool round trip for each configured provider
flywhl serve -w .
flywhl chat --model kimi:kimi-k2.6   # also dashscope:qwen3.8-flash, minimax:MiniMax-M2.7, ollama:qwen3.5:9b-mlx
```

For Anthropic's official SDK, install the extra with `pipx install 'flywhl-harness[anthropic]'`, then use
`--model anthropic:claude-opus-5-5`.

The access token is on by default, even on loopback. It is stored in `~/.flywhl/server_token` and survives restarts.
The first run in a directory asks once whether you trust the workspace. Saying no still works; writes and external actions after the
agent has read files then need a person. `--trust` skips the question.

## Default security posture

The goal is that opening an untrusted repository is safe with no configuration. bash, command hooks, stdio MCP servers and
`verify_command` all run in an OS sandbox: `sandbox-exec` on macOS, bubblewrap on Linux.

**What it stops**

- Reading credential locations such as `~/.ssh` and cloud config, enforced by the kernel on real paths.
- Writing outside the workspace and temp directories.
- Reaching the network, unless the host is on the allowlist. The list starts empty, so this is effectively offline until you add a host, and a
  blocked call names the host and how to allow it.
- Local IPC that would bypass the sandbox: keychain, clipboard, `open`, launchd, Unix sockets.
- A repository's own config turning any of this off. Only your user config, a file passed with `-c`, or `--sandbox off` can do that.
- A repository's `AGENTS.md` gaining the authority of the system prompt. Project files run in a separate labelled message, and until you trust the workspace, what the agent reads there taints the session, so later writes need a person. The terminal asks once, `--trust` records it, and non-interactive runs stay untrusted.

If no sandbox is available, the harness refuses to run commands. It does not quietly fall back.

**What it does not stop**

- Things written into the workspace that you later run outside the sandbox: a `Makefile`, `.envrc` or `package.json` script.
- Data leaving through a host you allowed. The workspace is readable, so allowing a host anyone can upload to allows exfiltration.
- Kernel exploits. The sandbox shares the host kernel. It contains mistakes and injection-driven overreach, and it is not a boundary for
  multi-tenant use.

**Everyday consequences**

```bash
flywhl chat --net-allow pypi.org --net-allow files.pythonhosted.org   # let the model install packages
```

Commands that need your credentials (`git push` over SSH, `gh`, `kubectl`) fail in the sandbox. Run them in your own terminal, or let the
model request `outside_sandbox: true`, a separate human gate: `--yes`, `auto_approve`, rules and session grants cannot pass it. Unattended
runs refuse it unless you name the command up front with `flywhl run --approve-outside '<pattern>'`. A command a person approved, on the
spot or by pattern, keeps `SSH_AUTH_SOCK` so `git push` can reach your ssh-agent; tokens such as `GH_TOKEN` are still stripped.
`.git/hooks` and `.git/config` are read-only in the sandbox, so `git add` and `git commit` work while `git config`, `git remote add` and
`git push -u` do not.

Details: [workspace and sandbox](https://flywhlway.github.io/flywhl-harness/en/governance/workspace-sandbox) ·
[behavior changes by version](https://github.com/flywhlway/flywhl-harness/blob/main/CHANGELOG.md)

## Use as a library

```python
import asyncio
from typing import Annotated
from flywhl_harness import Harness, HarnessConfig, HookEvent, HookOutput, Effect, tool

@tool(effect=Effect.READ)
async def weather(city: Annotated[str, "City name"]) -> str:
    """Look up the weather for a city."""
    return f"{city}: sunny, 23°C"

async def main():
    async with Harness(HarnessConfig.load(workspace=".")) as h:
        h.tools.register(weather)

        @h.hooks.on(HookEvent.PRE_TOOL_USE, matcher="bash")
        async def no_rm(inp):
            if "rm " in inp.tool_input["command"]:
                return HookOutput(decision="deny", reason="Deleting files is not allowed")

        result = await h.run("What's the weather in Shanghai?", on_event=print)
        print(result.status, result.text)

asyncio.run(main())
```

## Project status

Beta. The supported use is a local, single-user developer tool. It is not built for multi-user or multi-tenant service, and the console
should not be exposed beyond loopback.

- CI runs the offline suite on Linux (Python 3.11–3.13) and macOS, runs the sandbox tests for real on both, and installs the wheel into a
  clean environment to verify the sandbox boundaries end to end.
- 38 live scenarios against real models live in `tests/live`, including the web tools. They cost money and need keys, so they run outside CI, in a disposable container.
- Open security gaps are listed rather than hidden. In a workspace you have trusted, outside content it holds (an exported issue, say) does
  not count as tainted; trust is bound to the path, not the content; and the OS sandbox shares the host kernel. See the
  [security chapter](https://flywhlway.github.io/flywhl-harness/en/governance/security) and the
  [readiness review](https://github.com/flywhlway/flywhl-harness/blob/main/docs/quality-audit/release-readiness.md).
- The [first benchmark](https://flywhlway.github.io/flywhl-harness/en/benchmark/) covers 5 models and 30 small tasks at one commit. It is evidence for the harness mechanisms, not a claim about task success rates in real work.

## Documentation

- [English docs](https://flywhlway.github.io/flywhl-harness/en/): quick start, architecture, and a summary of all 30 chapters
- [Chinese tutorial](https://flywhlway.github.io/flywhl-harness/): the full text
- [Changelog](https://github.com/flywhlway/flywhl-harness/blob/main/CHANGELOG.md)

To read the code or run the examples and tests, clone the repository. The examples are not in the wheel.

```bash
git clone https://github.com/flywhlway/flywhl-harness && cd flywhl-harness
uv sync --all-extras
.venv/bin/python examples/11_kill9_cli_recover.py    # kill -9 a run, take over from another process, verify 7 invariants
.venv/bin/python -m pytest -q                        # runs inside an OS sandbox, in two stages
```

MIT License
