# 29 Compared with pi

*Summary. [Read the full chapter in Chinese](/advanced/pi-comparison).*

## The problem

pi is a small, well-known open-source coding agent. flywhl-harness borrowed several designs from it: event naming, the steer and follow-up queues, the compaction cut point, content and details on tool results. Reading the two side by side shows which parts of a harness are essential and which are product choices. The chapter's statements about pi come from a survey of pi's main branch and defer to pi's repository for details.

## Where the two differ, and why

| Area | pi | flywhl-harness | Reason given |
|---|---|---|---|
| Loop | Two nested `while` loops: inner for tool turns and steering, outer for follow-ups | One loop, `AgentLoop._body`; queued input at stop time goes through one `_drain_inbox()` | One drain point is easier to reason about, and input arriving during the Stop check is handled, not dropped |
| Tool parallelism | Parallel by default, same-path writes serialized | Only adjacent read-only calls batch together; writes run alone, in order | Writes take a snapshot, write the journal and may wait for approval, so serial order keeps journal order equal to side-effect order |
| Compaction trigger | Window minus 16384 | `min(window * 0.80, window - 8192)` | Proportional trigger on large windows; on a 1M window pi's formula fires near 98% |
| Compaction keep | 20000 tokens | `keep_recent_tokens` 12000, capped at 30% of the window (2000 when forced) | A fixed 20K keep collides with a 24K window |
| Permissions | None by default; run in a container | `default` mode: reads pass, writes and commands need approval | flywhl demonstrates a service deployment (web UI, REST API) |
| Subagents | Not built in; start another pi process through bash | Built-in `task` tool: isolated context, return contract, budget split, depth limit | The tutorial needs to explain these mechanisms |
| Planning | Files such as `PLAN.md` | `plan` mode, `todo_write`, Stop plan check | Same |
| MCP | Added as an extension in 0.99.0, with tool search and code-mode | Minimal stdio client, tools only | Both aim to limit context cost |
| Persistence | JSONL session file | SQLite: entries, steps, events, runs, approvals, blobs | Crash recovery needs a step journal, a state machine with leases and monotonic event seq |
| Extension surface | Dozens of events; extensions can rewrite context and requests | 8 hook points, each tied to a governance decision | Rewriting context threatens prefix stability |
| `edit` | Multiple replacements, fuzzy matching fallback | Exact match; reports line numbers when not unique; no fuzzy replacement | The chapter gives no reason |

The core ideas are shared. Both inject a steer at a safe point and a follow-up when the run would stop. Both fail the whole tool batch when output is cut off by `max_tokens`. Both never cut compaction at a tool result. Both give skills as a name, description and location list. The chapter's summary: pi shows the core loop can be tiny, and flywhl wraps the same small loop in a runtime and governance layer.

**Compaction by window size.** The compaction table in the chapter is worth redoing for your own window. At 24K, pi's trigger is 7,616 and its 20K keep exceeds it. At 128K, flywhl triggers at 102,400 and pi at 111,616. flywhl also clears old large tool results first, above 55% usage (`clear_threshold`). That step is turned off for models marked `append_only`, to keep the cached prefix intact.

**Stop-time input.** Input that arrives during the last model call is drained before the Stop check, and again after it, because the check may run tests. If a run ends paused, queued input is saved with the checkpoint. If it ends any other way, an `inbox_dropped` notice says what was not handled.

## pi's reasons for leaving things out

From the author's late-2025 post, as the chapter paraphrases it: no MCP because tool descriptions sit in context permanently, no subagents because their inner work is a black box, no plan mode because a file is more transparent, no permissions because confirmation does not stop an agent that can run arbitrary bash. The common rule is to use an existing general mechanism (files, bash, processes) before adding a dedicated one. By 0.99.0 pi had added MCP with tool search to manage the context cost, so the objection was about cost, not about MCP itself.

## Key code

| Where | What it does |
|---|---|
| `agent/loop.py` `AgentLoop._drain_inbox()` | Steer first, then follow-up, at stop time |
| `agent/inbox.py` `Inbox` | Two queues; `one_at_a_time` or `all` (default `all`) |
| `agent/compaction.py` `find_cut_index()`, `Compactor.compact()` | Cut point and keep rule |
| `agent/executor.py` `ToolExecutor.run_batch()` | Batching of adjacent read-only calls |
| `llm/base.py` `prepare_history()` | Synthetic results for orphaned tool calls |
| `tools/fs.py` `MAX_LINES`, `MAX_BYTES` | `read` keeps the head: 2000 lines or 50 KB |
| `tools/shell.py` `OUT_LINES`, `OUT_BYTES` | `bash` keeps the tail: 400 lines or 30 KB |
| `runtime/session.py` `render_summary()` | Renders the structured compaction summary |

## Related chapters

- [07 Context engineering](/en/core/context-engineering) covers compaction and clearing in full.
- [15 Streaming, steering and resumption](/en/reliability/streaming) covers the steer and follow-up queues.
- [27 Protocols: MCP, A2A and Skills](/en/advanced/protocols) covers the MCP client and the skills list.
- [28 The removable harness](/en/advanced/evolving-harness) covers which components a stronger model makes unnecessary.
