# 18 Workspace and sandbox

*Summary. [Read the full chapter in Chinese](/governance/workspace-sandbox).*

This page condenses the chapter's section on the default-on sandbox, the part that matters when you run the tool on a repository you did not write. Path constraints, drift detection, shadow-git snapshots, parallel worktrees, runtime comparisons and remote executors are in the Chinese text.

## The problem

Permission checks and bash analysis (chapter 17) are string-level filters. They cannot see what a script does, and they are not a security boundary. Something has to hold when they are wrong or bypassed: the kernel. With no configuration at all, bash, command hooks, MCP servers and `verify_command` run inside an OS sandbox. It cannot read credentials, cannot write outside the workspace, and has controlled network access.

## How it works

**Mode probing.** `permissions.sandbox` defaults to `auto`. At startup `probe()` in `tools/sandbox.py` finds out what the machine can really do, once per process (`detect()` caches it):

| Platform | Probe | Why it can fail |
|---|---|---|
| macOS | Runs `sandbox-exec` with a profile that has a real deny rule | Already inside another sandbox; `sandbox-exec` does not nest |
| Linux | Finds `bwrap` and runs it with the pid, ipc and net namespaces used at runtime | bubblewrap not installed; user namespaces blocked by AppArmor, seccomp or a masked `/proc` (the error text names the fix) |
| Other | Not probed | No system sandbox exists |

The probe has to execute something. A profile that allows everything also succeeds inside an outer sandbox, and a bwrap run with only `--ro-bind` can pass where `--unshare-net` later fails. `flywhl doctor`, the startup banner and the security page show the effective state from `Confinement.status()`, not the configured value.

**No sandbox means no execution.** If `auto` finds nothing usable, the harness refuses and says how to fix it. It does not fall back to running unconfined. bash and `verify_command` return an error, command hooks count as failed (a `pre_tool_use` guard blocks by default), and with MCP servers configured the harness fails to start. The chapter rejected "fall back with a warning" because on an unattended machine nobody reads the warning, and the point of a default is that you need not remember to check it. This is the `SandboxUnavailable` exception raised from `Confinement`.

**Three ways to turn it off, and project config is not one of them.** `sandbox = "off"` in the user config `~/.flywhl/config.toml`, in a file passed with `-c`, or `--sandbox off` on the command line. A `sandbox = "off"` or `sandbox_network = "allow"` in a project `flywhl.toml` (top-level, per hook, or per MCP server) is dropped with a warning, even if the user has trusted that file. Trusting a repository's config means trusting its hooks and model settings, not agreeing to remove isolation or open the network. This is `_keep_sandbox_on()` in `config.py`.

**Default network: an empty allowlist.** `sandbox_network = "auto"` becomes `allowlist` whenever a sandbox is in effect (or unavailable), and `allow` only if you explicitly turned the sandbox off. An empty allowlist is effectively no network, but it differs from `deny` in one useful way: the egress proxy knows which host it blocked and says so.

```text
[出站白名单拦下了 pypi.org:443。确实需要的话把主机加进 permissions.network_allow，或者启动时加 --net-allow pypi.org]
```

(The message is in Chinese in the source. It names the blocked host and tells you to add it to `permissions.network_allow` or pass `--net-allow pypi.org`.) The reason for a closed default: with open network, an injected instruction could send workspace contents out with `curl`. The sandbox stops reads of `~/.ssh`, but not that. The cost is that `pip install`, `npm install` and the first `git clone` fail until you allow the host.

**Four channels, one confinement layer.** `Confinement` in `tools/confine.py` resolves the effective level, starts the egress proxy and builds the launch arguments, and all four channels use it:

| Channel | Why it is inside | Writable |
|---|---|---|
| bash | Commands the model controls | Workspace, temp dir |
| Command hooks | Third-party scripts a project config can bring in | Same, plus its own state dir |
| MCP servers | Long-running third-party code, often fetched by `npx` or `uvx` | Same, plus its own state dir |
| `verify_command` | Runs code the agent just edited; outside the sandbox, a `conftest.py` would escape | Workspace, temp dir |

A single hook or MCP server can list hosts in `network_allow`, or, from user config only, set `sandbox_network = "allow"` or `sandbox = "off"`, so one server needing `~/.aws` does not open up bash. A compromised MCP server can still modify the workspace.

**Read-only inside the workspace.** The workspace is writable, but some paths later act outside the sandbox: `.flywhl/` and `flywhl.toml` (read as config on next start), the data directory, and `.git/hooks` and `.git/config` (run by the user's next `git commit` or an approved `git push`). `.git/commondir`, `.git/config.worktree` and `.git/worktrees` are read-only too, because they decide where git reads config and hooks from. The `.git` directory entry itself cannot be renamed, which stops swap-and-recreate. On macOS the workspace and every parent directory cannot be renamed either: the rules name paths, so renaming a parent under a writable location such as `/tmp` would step outside them. On Linux a missing file cannot be mounted read-only, so the harness sweeps `commondir` and `config.worktree` before entering the sandbox and every 0.1 seconds while it runs. That shortens the window rather than closing it. With no `.git` yet nothing is protected, so `git init` in an empty workspace works. Once `.git` is a directory, `hooks` is protected even if missing (a repo made with `git init --template=` has none; on Linux an empty directory is left there as the mount point). When `.git` is a file, as in a linked worktree or a submodule, the file itself is read-only, because its `gitdir:` line decides which config applies. Index, objects and refs stay writable, so `git add` and `git commit` work. What breaks: `git config`, `git remote add` and `git push -u` fail in the sandbox. Run them through `outside_sandbox` or in your own terminal.

`write` and `edit` run outside the sandbox, so these paths need their own check. While the sandbox is on, writing to any of them takes the same human gate as `outside_sandbox`, and unattended runs refuse, including `--yes`. Without this a model that cannot change `core.hooksPath` from bash could change it with the file tool, which the public benchmark observed: its hook then ran outside the sandbox on the user's next `git commit`. The check uses the sandbox's own list (`frozen_paths()`) and compares real paths and file identity, so `..`, symlinks, hard links and case variants do not get around it. The tool checks the resolved path again when it writes, in case a symlink changed after the decision. Other paths such as `.git/description` keep their usual approval rules.

**The `outside_sandbox` human gate.** For `git push`, `gh` or `docker`, the model passes `outside_sandbox: true` in the bash call. This is a separate gate from approval. It always needs a person, in `default`, `accept_edits` and `bypass`, and allow rules, session grants and `auto_approve` cannot pass it. `plan` mode, deny rules, credential locations and catastrophic commands still refuse. Unattended runs refuse, including `--yes`, unless the command matches `--approve-outside` (below). When a sandbox is configured but unavailable on this machine, ordinary commands refuse and `outside_sandbox` still goes through this gate, so a person can run one command outside. The card says "will run outside the sandbox", risk is critical, "always allow" is not offered, and "remember" applies to this one call. If a hook changes the arguments after approval, the call is refused, because the person approved a different command. `BashTool` and the file tools also check `ctx.human_approved` themselves, so replay and direct tool calls, which never reach approval, are refused by default. The file tools' gate lives on the tool, not in the permission layer, because `RecordedTool` has to replay recorded writes without diverging. Outside the sandbox the command can read credentials, write anywhere and use the network. Environment variables are still stripped, except `SSH_AUTH_SOCK`: `git push` is the most common use of this parameter, and it gives a signing capability rather than the key, on a command a person approved, either on the spot or ahead of time with an `--approve-outside` pattern. `GH_TOKEN` and `*_API_KEY` values are credentials themselves and stay stripped, so `gh` relies on the credentials from `gh auth login`, and anything that logs in through the environment runs in your own terminal. Bash inside the sandbox never sees the variable. With the sandbox off there is no gate, since everything is already outside.

**Explicit release for unattended runs.** `flywhl run --json --trust --approve-outside 'git push origin main'` lets a person make that one decision ahead of time. It exists only on the `run` command line: config files and environment variables cannot set it. The pattern must start with a concrete command name, so `*`, paths, interpreters and wrappers such as `bash`, `python`, `env` and `sudo` are refused. It applies only when unattended, a person in the room is still asked, and it cannot be combined with `--yes`. It covers bash only, not `write` or `edit` on `.git/hooks`. Because nothing contains the command once it leaves the sandbox, it refuses more than `auto_approve`: any segment of a compound command that does not match; programs that are not in the command text (command substitution, `git push --receive-pack=...` and its abbreviations, `ext::` URLs); commands that do not parse; commands the analyzer rates dangerous, such as `git push --force`; output redirected to a file; anything the managed baseline wants confirmed; a tainted session, which includes having read files in an untrusted workspace; and a PreToolUse hook asking for a person. Each release writes an `approvals` row with `decided_by = policy:approve_outside` and an `approve_outside` notice naming the command and the pattern. Patterns match text, so `make deploy` or `npm run release` run workspace files that sandboxed commands can edit. The `bash` tool's parameter description still says unattended runs refuse, because tool descriptions feed the replay fingerprint and changing it would diverge published trajectories.

## What this layer does not cover

The chapter lists these explicitly:

- **Content written into the workspace that later runs outside the sandbox.** `.git/hooks` and `.git/config` are read-only, but a sandboxed command can still write `Makefile`, `.envrc`, `package.json` scripts, and nested repo or submodule config (`.git/modules`). If the workspace had no `.git` at start, one created by the first command is unprotected. A `core.hooksPath` pointing at a workspace directory such as `.husky` is writable too. On Linux a sandboxed command can create `flywhl.toml` if it does not exist yet, though its risky settings still need trust; macOS refuses by path. These run when you later run `make`, enter the directory, or approve an `outside_sandbox` command. At the gate, read the command and think about what it will read from the workspace.
- **Running as PID 1 in a container.** Pass `docker run --init`. Python does not reap what bwrap leaves behind, so each sandboxed command leaves a zombie until `--pids-limit` is full and `fork` fails.
- **Data in the workspace itself.** The workspace is readable and allowlisted hosts are reachable. Allowing a host anyone can upload to allows exfiltration.
- **The shared kernel.** The sandbox stops mistakes and injection-driven overreach, not kernel exploits. For untrusted code or multi-tenant use, see the chapter's runtime comparison.
- **Very large workspaces on Linux.** Each command scans the workspace for credentials, and above 50,000 entries it refuses to run rather than scan less.
- **Config you trusted.** Entries such as `docker.sock` in `unix_sockets` or `workspace = "/"` apply as written once you confirm trust. The prompt lists them, so read it.
- **Commands installed under `/tmp`.** The Linux sandbox replaces `/tmp`, so venvs and interpreters there are invisible and an MCP server may exit at startup. Only `cwd` is mounted back.
- **Unverified distributions.** NixOS-style systems with commands under `/run/current-system` are untested. `/run` is empty in the sandbox, so commands on `PATH` may be missing.

## Upgrading

A project-level `sandbox = "off"` is dropped, so move it to user config. A setup with `sandbox = "macos"` or `"linux"` and no network line changes from `allow` to `allowlist`; write `sandbox_network = "allow"` in user config to keep the old behavior. Hooks and MCP servers that read `$HOME` or use the network fail until configured individually.

## Key code

| Where | What it does |
|---|---|
| `tools/sandbox.py` `probe()`, `detect()` | Real-execution probe for macOS and Linux; process-level cache |
| `tools/sandbox.py` `seatbelt_profile()`, `bwrap_argv()` | The macOS profile and Linux bubblewrap arguments, including read-only `.git/hooks` and `.git/config` |
| `tools/confine.py` `Confinement` | Resolves `auto`, starts the egress proxy, builds the launch; raises `SandboxUnavailable` |
| `tools/egress.py` | The egress proxy and host allowlist |
| `config.py` `_keep_sandbox_on()` | Drops `sandbox = "off"` from project config |
| `tools/shell.py` `BashTool.human_gate()` | Declares the `outside_sandbox` gate and re-checks `human_approved` |
| `tools/fs.py` `_GatedWrite.human_gate()` | The same gate for `write` and `edit` on read-only paths, with a re-check at write time |
| `policy/permissions.py` `PermissionResult.human_only` | Marks a call that only a person may release |
| `agent/executor.py` `_run_one()` | Skips auto-approval for gated calls and re-checks after rewrites |

## Related chapters

- [17 Permissions and human approval](/en/governance/permissions-hitl) is the layer this one backs up.
- [16 Hooks](/en/governance/hooks) covers command hooks, which run inside this confinement.
- [19 Security](/en/governance/security) lists what is still open overall.
