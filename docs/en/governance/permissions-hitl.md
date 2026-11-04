# 17 Permissions and human approval

*Summary. [Read the full chapter in Chinese](/governance/permissions-hitl).*

## The problem

Before each tool call the harness must answer: do it, ask a person, or refuse. A flat allowlist fails in both directions. It asks about `ls` a hundred times, and it lets `git status && rm -rf x` through because the string starts with `git status`. The chapter's stance is that permissions are one layer of defense in depth, meant to give most calls the right default. They are not the final security boundary. The sandbox (chapter 18) is.

## How it works

**The decision pipeline.** `PermissionPolicy.evaluate()` runs in this order:

1. Bash commands are analyzed first. A command flagged `catastrophic` (deleting `/` or the home directory or its first-level subdirectories, `mkfs`, writing to a block device, a fork bomb) or `credential` (touching `~/.ssh`, `~/.kube/config` and similar) is denied in every mode. `bypass`, allow rules and "always allow this session" grants do not reach that check, and approval never offers it, because a tired reviewer clicking "approve" on `rm -rf ~` cannot be undone.
2. Rules are matched deny, then ask, then allow, and the first match wins. Rule source (managed, project, user, session) does not affect order, so a user's `allow:bash` cannot beat a managed deny.
3. With no rule match, the mode decides. The four modes are `default`, `accept_edits`, `plan` and `bypass`. `plan` masks write tools at execution rather than removing their definitions, so switching modes does not break the prefix cache. In `plan`, rules can only tighten.
4. Two escalations only ever go up: a `dangerous` command that was allowed becomes ask, and so does any non-read operation once the session is tainted. Neither applies in `bypass`.
5. `pre_tool_use` hooks run, and the strictest answer wins.

**Compound commands.** For bash, an allow rule must cover every segment. `allow:bash(git status*)` does not admit `git status && rm -rf x`. A deny or ask on any one segment is enough. Commands containing `$(...)` or backticks are marked opaque and drop from allow to ask, because the substituted command is not covered by the rule.

**The analyzer.** `analyze()` in `policy/bash_analyzer.py` tokenizes with `shlex` and classifies each segment instead of running regexes over the whole line. It strips wrappers (`env`, `timeout`, `xargs`, `sudo`), recurses into `sh -c` and `eval` (depth limit 3), expands braces, tracks variables assigned in the command, and treats paths outside the workspace as needing confirmation even for read-only commands. The chapter is explicit that it is not a security boundary. What remains unclosed depends on runtime state: `D=$(echo $HOME/.ku); cat ${D}be/config` is not recognized as a credential read, and `python3 script.py` hides what the script does. The analyzer's real job is cutting approval fatigue on `ls` and `git status`.

**Taint.** A session becomes tainted after a bash command with a `network` flag (`curl`, `wget`, `git pull`, `npm install`), an MCP tool that is not declared closed, or a subagent that read untrusted content. The flag is stored in session metadata, so pause, resume and crash takeover keep it. It is deliberately coarse and only ever set, never cleared. Gaps the chapter lists: workspace files count as trusted, and taint does nothing under `bypass` or `on_ask="allow"`.

**Approvals are durable records.** An `ask` creates an `ApprovalRecord` and moves the run to `waiting_human`. It is not a blocking HTTP request, so a reloaded browser still sees it. Resolution is one compare-and-swap (`WHERE status='pending'`), so approve, timeout and cancel racing each other produce exactly one outcome, and a human decision that arrives with the timeout wins. Timeout (default 600 s) means reject, because nobody looking is not consent. After a crash, recovery expires pending approvals and the call asks again.

**What the person approves is the real action.** The card shows actual arguments, a summary generated from them, the analyzer's reasons and, for `write` and `edit`, a unified diff preview (best effort, truncated). Model-written descriptions are never shown as the action. If the reviewer edits the arguments, they are re-validated, re-judged by permissions (TOCTOU) and sent through `pre_tool_use` hooks again. A rejection reason typed by a person reaches the model as a `<system-reminder>` rather than inside the tool result: the system prompt treats tool results as data, so a reason placed there can be ignored as untrusted text.

**Unattended runs.** `on_ask` is `wait`, `allow` or `deny`. `allow` approves everything the policy would ask about, leaving only deny rules, so the chapter recommends `on_ask="deny"` plus `auto_approve` patterns. Even a broad `auto_approve` does not cover dangerous commands, commands whose real program is not in the text, baseline-protected locations, paths outside the workspace, hook-requested asks, or external side effects after taint.

**Approval fatigue.** "Always allow this session" creates a rule via `grant_for()`: the first two words plus `*` for bash. The chapter points out this is wide: `npm install left-pad` becomes `allow:bash(npm install*)`. `policy/fatigue.py` raises a notice after repeated sub-two-second approvals or three approvals of the same grant.

**Permissions versus containers.** The chapter sets this against pi's position that permission prompts cannot stop a determined agent and a container should be used instead. A container limits local damage but cannot recall an email or a push. Approval acts before the action. The two are a division of labor.

## Key code

| Where | What it does |
|---|---|
| `policy/permissions.py` `PermissionPolicy.evaluate()` | The decision pipeline and escalations |
| `policy/permissions.py` `MANAGED_BASELINE`, `Rule`, `Mode` | Baseline rules, rule syntax, the four modes |
| `policy/permissions.py` `grant_for()`, `auto_approve_rule()` | Session grants and unattended auto-approval rules |
| `policy/bash_analyzer.py` `analyze()` | Segment classification, wrappers, recursion, flags |
| `policy/approvals.py` `ApprovalBroker` | Create, wait, time out, cancel an approval |
| `runtime/store.py` `ApprovalRecord` | The persisted approval and its single-shot resolution |
| `policy/fatigue.py` `FatigueMonitor` | Rubber-stamp and repeated-grant notices |
| `config.py` `PermissionConfig` | `on_ask`, `approval_timeout`, `auto_approve` |

## Related chapters

- [16 Hooks](/en/governance/hooks) can add `ask` or `deny` but never loosen a rule.
- [18 Workspace and sandbox](/en/governance/workspace-sandbox) is the layer that holds when this one is bypassed.
- [19 Security](/en/governance/security) covers injection, which taint is built for.
