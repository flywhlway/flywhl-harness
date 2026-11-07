# 27 Protocols: MCP, A2A and Skills

*Summary. [Read the full chapter in Chinese](/advanced/protocols).*

## The problem

Function Calling, MCP, A2A, Agent Skills and AGENTS.md are often lumped together, but they sit at different layers. Mixing them up leads to wrong security assumptions and wasted context.

| Mechanism | Connects | Carries | Who executes | In this repo |
|---|---|---|---|---|
| Function Calling | model and harness | tool schemas, call intent, results | the harness | provider request and stream code under `llm/` |
| MCP | harness and tool server | tools, resources, prompts | the MCP server | `tools/mcp.py`, tools only |
| A2A | agent and agent | task delegation, status, artifacts | a remote agent, as a black box | not implemented |
| Agent Skills | files to model | reusable instructions and scripts | the model, with existing tools | `prompts.py` `find_skills()` |
| AGENTS.md | files to model | project instructions | the model | `prompts.py` `find_project_context()` |

An MCP tool still reaches the model as an ordinary tool schema. The model cannot tell a local tool from an MCP one, so the harness has to carry the trust difference itself.

## How it works

**The MCP client** is a stdlib-and-asyncio client for the stdio transport, tools only.

- It spawns the server with a scrubbed environment: variables whose names match `KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH` are removed, then the configured `env` is layered on.
- A background `_read_loop` routes responses to pending futures by `id`, so requests can overlap on one process. Server-initiated requests always get a reply: `ping` gets an empty result and everything else gets `-32601`. A server that needs sampling or roots fails fast instead of waiting forever.
- `tools/call` is sent with no client timeout. `ToolExecutor` owns timing, and on timeout or cancel the client sends `notifications/cancelled`. MCP tools therefore share the local timeout semantics, including `unknown` for a timed-out non-idempotent external call.

**Mapping MCP tools to harness tools** is where the safety decisions live. Names become `mcp__<server>__<tool>`, so permission rules can use globs such as `deny:mcp__github__*`. Descriptions are cut to 2000 characters and tagged with their source. Tool annotations are hints, not guarantees, so they are ignored unless the server config sets `trust_annotations = true`. Without it:

- A tool is `read` only if its name matches the server's `read_only_tools`; otherwise it is `EXTERNAL` and needs approval.
- Output is marked untrusted (tainted), so later side effects escalate to approval.
- The tool is not idempotent, so crash recovery marks it `unknown` rather than redoing it.

`load_mcp_tools` sorts tools by name so the tool prefix stays stable for prompt caching. The tool set is frozen per session at creation.

**Not implemented:** Streamable HTTP, resources and prompts, client capabilities such as sampling, and `reconcile` for in-flight MCP calls. The chapter also notes that large tool sets crowd the context and lists remedies (tool search, code-mode, CLI plus README, per-session tool sets). The repo only truncates descriptions and fixes the order.

**Skills** use progressive disclosure. `find_skills()` scans `.flywhl/skills/*/SKILL.md` and `skills/*/SKILL.md` in the workspace, reads the first 2000 characters and parses single-line `key: value` frontmatter. The list goes into the system prompt as `<available_skills>`, and the model reads the full `SKILL.md` on demand. Multi-line YAML values are not parsed. The list is frozen with the session. Skill scripts run through `bash`, so permissions still apply.

**AGENTS.md** is gathered by `find_project_context()`. It walks up from the workspace to the git root, takes one of `AGENTS.md` or `CLAUDE.md` per directory, and enforces a 32 KiB limit counted in UTF-8 bytes. Nearer files win when the budget runs out, and the output order puts ancestors first. Outside a git repo only the workspace itself is read. These files bypass the permission layer, so a symlink pointing outside the project or at a secret is skipped with a warning.

**A2A** is covered as a design. A Run maps closely onto an A2A task: `waiting_human` is input-required, `running` is working, `Harness.subscribe(run_id, after_seq)` gives resubscription. As a client, a remote agent would be an `EXTERNAL`, untrusted-output tool whose `reconcile()` finds the remote task again by idempotency key.

## Key code

| Where | What it does |
|---|---|
| `tools/mcp.py` `MCPClient` | Handshake, request multiplexing, cancel notification, `tools/list` pagination |
| `tools/mcp.py` `MCPTool` | Name prefix, effect mapping, annotation distrust, content conversion |
| `tools/mcp.py` `load_mcp_tools()` | Starts configured servers; sorts tools; reads `read_only_tools` and `trust_annotations` |
| `tools/shell.py` `_clean_env()` | Strips secret-looking variables from the server environment |
| `prompts.py` `find_skills()` | Skill discovery and the `<available_skills>` list |
| `prompts.py` `find_project_context()` | Layered AGENTS.md loading with a byte budget |
| `examples/mcp_echo_server.py` | A minimal stdio server for trying the setup |

## Related chapters

- [06 The tool system](/en/core/tools) defines effect levels and untrusted output.
- [19 Security: injection, credentials, supply chain](/en/governance/security) gives the threat model behind annotation distrust.
- [12 Cancellation semantics](/en/reliability/cancellation) explains why `tools/call` carries no client timeout.
- [26 Edge-cloud collaboration](/en/advanced/edge-cloud) uses MCP for a local daemon.
