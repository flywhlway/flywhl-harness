# 19 Security: injection, credentials, supply chain

*Summary. [Read the full chapter in Chinese](/governance/security).*

This page is for deciding whether to trust the tool. It states the defenses, then the gaps the chapter itself admits.

## The problem

A model treats any text it reads as a potential instruction. The attack surface is therefore everything that can enter the context, what the context can drive, and what that can send out. Entry points the chapter walks through: user input, project instruction files (`AGENTS.md`, `CLAUDE.md`), skill descriptions, workspace files, web pages and issues, MCP tool descriptions and results, and subagent return values. Risk peaks when three things coexist: access to private data, exposure to untrusted content, and a way to communicate outward.

Prompt injection cannot be fixed the way SQL injection was, because the model has no syntax-level split between code and data. The chapter sets two goals: lower the success rate, and limit the damage after success. The second matters more because it does not depend on model behavior.

## How it works

**Taint.** After a session reads untrusted content, side-effecting operations escalate to a human. Sources: bash commands the analyzer flags `network` (`BashTool.untrusted_for`), all MCP output unless the server is configured `trust_annotations` and declares `openWorldHint: false`, and subagents (a tainted child run taints the parent). Taint is stored in session metadata and survives pause, resume and takeover. Compaction summaries lose per-message provenance, but the session flag still applies.

**MCP annotations are not believed by default.** A server claiming `readOnlyHint` or `idempotentHint` could skip approval or get calls replayed. Without `trust_annotations`, all three hints are ignored (kept in `ignored_hints`, shown on the settings page). `read_only_tools` relaxes only the effect level and the output stays tainted.

**Instruction and data separation.** Runtime notes use `reminder()` as separate `<system-reminder>` messages. `ContextManager.shape_tool_result` escapes that tag, including case variants and attributes, in tool results before they enter context. The system prompt states that text in tool results, files and web pages is data. Tags in MCP tool descriptions and `AGENTS.md` are not escaped.

**Project config is split by risk.** `split_untrusted` in `config.py` lets model and limit settings take effect, plus anything that only tightens (deny/ask rules, `plan`, `on_ask="deny"`, a sandbox level other than off, `sandbox_network="deny"`). Hooks, MCP servers, `verify_command`, `system_prompt`, `append_system_prompt`, allow rules, `bypass`, `base_url` and the rest are ignored until the user trusts the file. Trust is recorded in `~/.flywhl/trusted.json` as the file's absolute path plus a SHA-256 of its content, and the hash also covers scripts the hook command or MCP launch line names. `sandbox = "off"` and `sandbox_network = "allow"` are dropped even when trusted, including per hook and per MCP server. User config and `-c` files are trusted. A project `.env` is loaded through an allowlist (`IMPLICIT_ENV_KEYS` in `cli.py`): provider keys and model selection only, so a cloned repo cannot redirect your API key with `OPENAI_BASE_URL`.

**The approval UI is an attack target.** Cards show real arguments, a diff preview and the analyzer's reason. The web server defends against DNS rebinding with a Host allowlist, rejects cross-site writes by `Origin`, requires an access token by default, and sends a CSP with `frame-ancestors 'none'` so the console cannot be framed to trick a click. Listening on a non-loopback address needs an explicit `--expose` and a token, and a manual `--token` there needs at least 16 characters. The Host allowlist cannot be switched off: on `0.0.0.0` and `::` it holds loopback names and `--allow-host` entries, a specific listen IP joins automatically, and `*` is refused. `--no-token` works on loopback only, and `serve` refuses it when commands the agent runs can reach the console port, because without a token they could approve their own human gate through `POST /api/approvals/{aid}`. That covers a sandbox level of off, `sandbox_network = "allow"`, an allowlist rule for that loopback port, and the same settings on a single hook or MCP server. A configured sandbox that is unavailable on the machine does not count, since the command never runs.

**Credentials.** bash, MCP server and command hook child processes have secret-like environment variables stripped. Needed ones are listed in the config `env` or `pass_env`, which trust confirmation shows. The design rule: keep tokens in dedicated harness-side tools (a "create PR" tool), not in the general execution surface. Short-lived scoped tokens and credential proxying are not implemented.

**Supply chain.** MCP servers, skills and hooks run with your privileges or enter the model context. Present: server-prefixed tool names (`mcp__<server>__*`), stripped environments, trust-before-start for project config, script content bound into the trust hash, unpinned packages launched via `npx`, `uvx`, `docker run` and the like flagged (refused under `require_pinned`), and servers' reverse requests answered only for `ping`. Absent: signing and permission declarations.

**Defenses as regression gates.** The chapter's first test layer assumes the model is already hijacked: a scripted model issues the attacker's calls and the test asserts that the harness refuses them, deterministically and in every CI run. `tests/test_security.py` follows this pattern, one case per closed gap.

## Remaining gaps

The chapter's summary names the main ones.

- **Workspace files are not a taint source.** `read` and `grep` results do not taint. An injection in a cloned third-party repo or an exported issue does not trigger escalation. The chapter calls this a deliberate trade-off, since treating the workspace as untrusted would taint almost every session. On a foreign repository, start in `plan` mode. The default sandbox still limits what a successful injection can reach.
- **Project instruction files reach the system prompt.** `build_system_prompt` puts `AGENTS.md`, `CLAUDE.md` and the skill list into it (capped at 32 KiB of UTF-8, oversize files skipped whole). On an unfamiliar repo the author gains system-prompt authority. Disable `load_agents_md` and `load_skills`, or review in `plan` mode first. A project config can only turn these two off. Links that leave the project or point at a credential location are skipped with a warning.
- **Shared kernel, and workspace content that later runs outside the sandbox.** The sandbox stops mistakes and injection-driven overreach, not kernel exploits. `Makefile`, `.envrc` and submodule config written inside the sandbox can run later outside it (see chapter 18).
- **Bash analysis is not a security boundary.** Anything that depends on runtime state, such as a value computed by command substitution or a script's contents, cannot be classified statically. The analyzer reduces approval fatigue and catches careless or simple cases. Determined obfuscation after an injection is for the sandbox to stop.
- **`on_ask="allow"` (`--yes`)** approves every ask except human-gated calls, leaving deny rules and that gate. Use it only in a disposable sandbox.
- **Output filtering** in `post_tool_use` does not reach streamed `tool_update` events.
- **Taint is coarse.** It covers the whole session and is only ever set, never cleared.
- **The approver identity** is recorded as `web-user`. A shared deployment needs real authentication in front.

## Key code

| Where | What it does |
|---|---|
| `tools/shell.py` `BashTool.untrusted_for()`, `_clean_env()` | Network taint; secret environment stripping |
| `tools/mcp.py` `MCPTool` | Annotation defaults, `ignored_hints`, taint for MCP output |
| `agent/context.py` `shape_tool_result()` | Escapes forged `<system-reminder>` tags |
| `prompts.py` `build_system_prompt()`, `find_project_context()` | Loads project instruction files under a byte cap |
| `config.py` `split_untrusted()` | Safe versus trust-required project settings |
| `policy/supply_chain.py` | Script references and content pins for trust hashes |
| `cli.py` `IMPLICIT_ENV_KEYS` | `.env` allowlist |
| `server/app.py` | Host check, cross-site write check, token, CSP |
| `server/exposure.py` | Startup checks: `--expose`, token length, `--no-token` reachability |

## Related chapters

- [16 Hooks](/en/governance/hooks), [17 Permissions and human approval](/en/governance/permissions-hitl) and [18 Workspace and sandbox](/en/governance/workspace-sandbox) hold the individual defenses.
- [20 Subagents](/en/multi-agent/subagents) covers the return contract and taint propagation.
- [Trajectory and evaluation](/en/multi-agent/trajectory-eval) covers the second, model-level test layer.
