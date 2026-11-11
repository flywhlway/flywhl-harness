# 19 Security: injection, credentials, supply chain

*Summary. [Read the full chapter in Chinese](/governance/security).*

This page is for deciding whether to trust the tool. It states the defenses, then the gaps the chapter itself admits.

## The problem

A model treats any text it reads as a potential instruction. The attack surface is therefore everything that can enter the context, what the context can drive, and what that can send out. Entry points the chapter walks through: user input, project instruction files (`AGENTS.md`, `CLAUDE.md`), skill descriptions, workspace files, web pages and issues, MCP tool descriptions and results, and subagent return values. Risk peaks when three things coexist: access to private data, exposure to untrusted content, and a way to communicate outward.

Prompt injection cannot be fixed the way SQL injection was, because the model has no syntax-level split between code and data. The chapter sets two goals: lower the success rate, and limit the damage after success. The second matters more because it does not depend on model behavior.

## How it works

**Taint.** After a session reads untrusted content, side-effecting operations escalate to a human. Sources: bash commands the analyzer flags `network` (`BashTool.untrusted_for`), all MCP output unless the server is configured `trust_annotations` and declares `openWorldHint: false`, web tool results (`web_search`, `web_fetch`, and vendor-side search once results appear), and subagents (a tainted child run taints the parent). Taint is stored in session metadata and survives pause, resume and takeover. Compaction summaries lose per-message provenance, but the session flag still applies.

**Workspace trust and the project channel.** `AGENTS.md`, `CLAUDE.md` and the skill list are written by the repository author, so they no longer sit in the system prompt. `build_project_context` wraps them in a `<project-context trust="...">` message that `_call_model` puts first in every request. It stays out of history, so compaction cannot touch it, and it is frozen at session creation. Trust is recorded per workspace under `workspaces` in `~/.flywhl/trusted.json`, bound to the git root (`trust_root`), so a nested repo in `vendor/` counts separately. A terminal run asks once, `--trust` records it, and non-interactive runs stay untrusted without prompting. User config or `-c` files can set `trust_workspace = true` for all workspaces; a project config cannot, even when trusted. In an untrusted workspace, instruction files are labelled `untrusted` and open the session tainted, and otherwise the first tool that reads workspace content (`read`, `grep`, `ls`, `edit`, `bash`, or failed `verify_command` output) taints it. Allow rules, session grants and `accept_edits` then stop passing writes without a person. `bypass` ignores taint. Subagents inherit the parent's taint, because the brief was written by the tainted parent.

**MCP annotations are not believed by default.** A server claiming `readOnlyHint` or `idempotentHint` could skip approval or get calls replayed. Without `trust_annotations`, all three hints are ignored (kept in `ignored_hints`, shown on the settings page). `read_only_tools` relaxes only the effect level and the output stays tainted.

**Instruction and data separation.** Runtime notes use `reminder()` as separate `<system-reminder>` messages. `ContextManager.shape_tool_result` escapes that tag, including case variants and attributes, in tool results before they enter context. The system prompt states that text in tool results, files and web pages is data. Project instruction files are escaped too, including `</project-context>` and `</file>`. Tags in MCP tool descriptions are not escaped.

**Project config is split by risk.** `split_untrusted` in `config.py` lets model and limit settings take effect, plus anything that only tightens (deny/ask rules, `plan`, `on_ask="deny"`, a sandbox level other than off, `sandbox_network="deny"`). Hooks, MCP servers, `verify_command`, `system_prompt`, `append_system_prompt`, allow rules, `bypass`, `base_url` and the rest are ignored until the user trusts the file. Trust is recorded in `~/.flywhl/trusted.json` as the file's absolute path plus a SHA-256 of its content, and the hash also covers scripts the hook command or MCP launch line names. `sandbox = "off"` and `sandbox_network = "allow"` are dropped even when trusted, including per hook and per MCP server. User config and `-c` files are trusted. A project `.env` is loaded through an allowlist (`IMPLICIT_ENV_KEYS` in `cli.py`): provider and web-search keys and model selection only, so a cloned repo cannot redirect your API key with `OPENAI_BASE_URL`.

**Web tools are a second egress channel.** `web_search` and `web_fetch` send HTTP from the harness process, outside the bash sandbox and its `network_allow` proxy, so search hosts are not added to `network_allow` (that would also let `curl` reach them). `net/client.py` is the only outbound HTTP path: it canonicalizes the URL once and checks and sends that same string, allows https only, resolves and vets every address (public only; loopback, private, link-local, cloud metadata and mapped forms are refused), pins the connection to the vetted IP against DNS rebinding, follows redirects manually and re-checks each hop, and sends no cookies, no Referer and ignores proxy variables. `fetch_scope` matches full URLs, query string included, against URLs the user typed or the session's search results returned, because host-level allowlisting lets an attacker hide data in the path or query of a trusted host. An exfiltration check (`net/exfil.py`) refuses queries and URLs that contain secret-like environment values, known key prefixes or long random strings, without asking a person. A project config can only tighten `[web]`: `enabled` and `native` only to `false`, and backends, endpoints and `fetch_allow` are dropped. Key variable names and endpoints never appear in the API, events or logs. Vendor-side search (`native = true`) puts results straight into the model context, so only confirmed vendors are enabled and declarations that cannot show whether a search happened taint the session from the start.

**The approval UI is an attack target.** Cards show real arguments, a diff preview and the analyzer's reason. The web server defends against DNS rebinding with a Host allowlist, rejects cross-site writes by `Origin`, requires an access token by default, and sends a CSP with `frame-ancestors 'none'` so the console cannot be framed to trick a click. Listening on a non-loopback address needs an explicit `--expose` and a token, and a manual `--token` there needs at least 16 characters. The Host allowlist cannot be switched off: on `0.0.0.0` and `::` it holds loopback names and `--allow-host` entries, a specific listen IP joins automatically, and `*` is refused. `--no-token` works on loopback only, and `serve` refuses it when commands the agent runs can reach the console port, because without a token they could approve their own human gate through `POST /api/approvals/{aid}`. That covers a sandbox level of off, `sandbox_network = "allow"`, an allowlist rule for that loopback port, and the same settings on a single hook or MCP server. A configured sandbox that is unavailable on the machine does not count, since the command never runs.

**Credentials.** bash, MCP server and command hook child processes have secret-like environment variables stripped. Needed ones are listed in the config `env` or `pass_env`, which trust confirmation shows. The design rule: keep tokens in dedicated harness-side tools (a "create PR" tool), not in the general execution surface. Short-lived scoped tokens and credential proxying are not implemented.

**Supply chain.** MCP servers, skills and hooks run with your privileges or enter the model context. Present: server-prefixed tool names (`mcp__<server>__*`), stripped environments, trust-before-start for project config, script content bound into the trust hash, unpinned packages launched via `npx`, `uvx`, `docker run` and the like flagged (refused under `require_pinned`), and servers' reverse requests answered only for `ping`. Absent: signing and permission declarations.

**Defenses as regression gates.** The chapter's first test layer assumes the model is already hijacked: a scripted model issues the attacker's calls and the test asserts that the harness refuses them, deterministically and in every CI run. `tests/test_security.py` follows this pattern, one case per closed gap.

## Remaining gaps

The chapter's summary names the main ones.

- **Trust binds to a path, not to content.** A workspace is trusted once, by repository root (or the directory itself outside a repo). Delete the repo and clone something else to the same path and the trust remains. Inside a trusted workspace, foreign text such as an exported issue does not taint.
- **Project instruction files still reach the model.** They run in a separate labelled channel, capped at 32 KiB of UTF-8 with oversize files skipped whole. The label asks the model to treat untrusted files as data, which helps but is not enforced. Disable `load_agents_md` and `load_skills`, or review in `plan` mode first. A project config can only turn these two off. Links that leave the project or point at a credential location are skipped with a warning.
- **Shared kernel, and workspace content that later runs outside the sandbox.** The sandbox stops mistakes and injection-driven overreach, not kernel exploits. `Makefile`, `.envrc` and submodule config written inside the sandbox can run later outside it (see chapter 18).
- **Bash analysis is not a security boundary.** Anything that depends on runtime state, such as a value computed by command substitution or a script's contents, cannot be classified statically. The analyzer reduces approval fatigue and catches careless or simple cases. Determined obfuscation after an injection is for the sandbox to stop.
- **`on_ask="allow"` (`--yes`)** approves every ask except human-gated calls, leaving deny rules and that gate. Use it only in a disposable sandbox.
- **Output filtering** in `post_tool_use` does not reach streamed `tool_update` events.
- **Taint is coarse.** It covers the whole session and is only ever set, never cleared.
- **The approver identity** is recorded as `web-user`. A shared deployment needs real authentication in front.
- **Web exfiltration checks are a backstop.** Deliberate encoding (base64, splitting a secret across calls) gets past them, and vendor-side search is outside `fetch_scope` entirely.

## Key code

| Where | What it does |
|---|---|
| `tools/shell.py` `BashTool.untrusted_for()`, `_clean_env()` | Network taint; secret environment stripping |
| `tools/mcp.py` `MCPTool` | Annotation defaults, `ignored_hints`, taint for MCP output |
| `agent/context.py` `shape_tool_result()` | Escapes forged `<system-reminder>` tags |
| `prompts.py` `build_project_context()`, `find_project_context()` | Project channel with trust label, under a byte cap |
| `config.py` `trust_root()`, `is_workspace_trusted()` | Workspace trust records |
| `config.py` `split_untrusted()` | Safe versus trust-required project settings |
| `policy/supply_chain.py` | Script references and content pins for trust hashes |
| `cli.py` `IMPLICIT_ENV_KEYS` | `.env` allowlist |
| `server/app.py` | Host check, cross-site write check, token, CSP |
| `server/exposure.py` | Startup checks: `--expose`, token length, `--no-token` reachability |
| `net/client.py` `WebClient`, `canonical_url()` | The single outbound HTTP path for web tools |
| `net/exfil.py` `leaked()`, `tools/web.py` `WebFetchTool.precheck()` | Exfiltration check and `fetch_scope` |

## Related chapters

- [16 Hooks](/en/governance/hooks), [17 Permissions and human approval](/en/governance/permissions-hitl) and [18 Workspace and sandbox](/en/governance/workspace-sandbox) hold the individual defenses.
- [20 Subagents](/en/multi-agent/subagents) covers the return contract and taint propagation.
- [Trajectory and evaluation](/en/multi-agent/trajectory-eval) covers the second, model-level test layer.
