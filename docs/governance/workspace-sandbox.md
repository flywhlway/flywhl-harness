# 18 Workspace 与沙箱

前两章回答“这个动作能不能做”，本章回答另一半：**动作落在哪里，出了错能退回到哪里，最坏情况下能伤到什么。**
Workspace 是 Agent 的工作现场，沙箱是这个现场的围墙。flywhl-harness 实现了前者的路径约束、漂移检测和影子 git 快照，
没有实现后者，本章最后说明怎样把 bash 接到远程沙箱上。

## Workspace 是什么

“一个目录”只是 Workspace 最显眼的部分。一个能让 Agent 持续工作、能暂停恢复、能回滚的现场至少包括：

| 组成 | 内容 | 本项目的对应 |
|---|---|---|
| 代码与文件 | 仓库、产物、临时文件 | `Workspace.root` |
| 依赖 | `node_modules`、`.venv`、系统包 | 与宿主共用，不做隔离 |
| 执行环境 | shell、解释器、环境变量、网络 | 宿主进程；bash 子进程剥离密钥环境变量 |
| 元数据 | 检查点清单、快照历史、会话数据 | `.flywhl/`：`harness.db`、`shadow.git`（数据目录在工作区外时为 `shadow-<hash>.git`） |
| 凭据 | 访问外部系统的身份 | 理想情况下不在现场内，见下文“凭据代持” |

生命周期：

```mermaid
stateDiagram-v2
    state "创建" as Created
    state "使用" as InUse
    state "快照" as Snapshot
    state "恢复" as Restored
    state "分叉" as Forked
    state "归档 / 销毁" as Archived
    [*] --> Created: Workspace 初始化，建根目录与数据目录
    Created --> InUse: 文件工具经 resolve()，bash 以根目录为 cwd
    InUse --> Snapshot: 每次非只读调用前 ShadowGit.snapshot()
    Snapshot --> InUse
    InUse --> Restored: rollback API 调用 ShadowGit.restore()
    Restored --> InUse
    InUse --> Forked: 生产实现，新 worktree 或卷快照克隆
    Forked --> InUse
    InUse --> Archived: 生产实现，打包上传并释放沙箱
    Archived --> [*]
```

本项目覆盖了创建、使用、快照、恢复四个阶段。“分叉”需要特别说明：会话树的分支（见 [09 状态与持久化](/reliability/state-persistence)）
只分叉 **对话**，不分叉 **文件**——在会话树上切回一个旧分支继续，工作区里的文件仍是最新状态。需要两者一致时，要配合“回滚到此”使用。
归档与销毁没有实现，`shadow.git` 会随使用持续增长。

## 路径约束

所有文件工具都通过 `Workspace.resolve()` 把模型给出的路径变成绝对路径：

```python
def resolve(self, path: str, *, allow_outside: bool = False) -> Path:
    """把模型给的路径解析为绝对路径，并做逃逸检查。

    ``Path.resolve()`` 会跟随符号链接，因此“工作区内的软链接指向 /etc”也会被识别为越界。
    """
    p = Path(os.path.expanduser(path))
    if not p.is_absolute():
        p = self.root / p
    p = p.resolve()
    if not allow_outside and not self.is_inside(p):
        raise PathEscape(f"路径 {path} 解析为 {p}，位于工作区 {self.root} 之外")
    return p
```

| 输入 | 解析结果 | 判定 |
|---|---|---|
| `src/a.py` | `<root>/src/a.py` | 工作区内 |
| `../../etc/passwd` | `/etc/passwd` | `PathEscape` |
| `~/.bashrc` | 家目录下的文件 | `PathEscape` |
| `etc_link/hosts`（`etc_link → /etc`） | `/etc/hosts`（macOS 上是 `/private/etc/hosts`） | `PathEscape`：`resolve()` 先跟随链接再比较 |
| `<root>/out.txt`（绝对路径） | 原样 | 工作区内 |

关键在于 **先 resolve、再比较**。如果先比较字符串前缀再打开文件，`<root>/etc_link/hosts` 会通过检查，打开时却读到 `/etc/hosts`。

`allow_outside=True` 只在权限判定时使用（`PermissionPolicy._targets` 需要知道工作区外路径的绝对形式，才能匹配规则或给出 ask）；
真正执行时不带这个参数，越界会抛出 `PathEscape`，`ToolExecutor._invoke` 把它转换成 `status="denied"` 的工具结果。

路径约束只是第一道门，它管不到的情况：

- **bash 不经过它。** 命令分析器对 bash 做了一层近似：参数里出现工作区外的绝对路径、`~`、`$HOME` 时，即使是 `cat /etc/hosts` 这样的只读命令
  也判为 external，default 模式下要确认、plan 模式下拒绝（见 [17 权限与人审](/governance/permissions-hitl)）；用 `..` 爬出工作区的相对路径
  （`cat ../../etc/hosts`）按 `normpath` 识别。但它只看字面参数，变量拼出来的路径（`D=/etc; cat $D/hosts`）、符号链接都识别不到、直接放行，
  和 `resolve()` 的“先解析再比较”不是一个强度。
- **硬链接。** 工作区内一个指向外部文件的硬链接，`resolve()` 看到的就是工作区内路径。`read` 会读到外部文件的内容；
  `write` / `edit` 因为走原子写（见下文），替换的是工作区里的目录项，外部文件不受影响——但 bash 里的 `echo >> link` 仍会改到它。
- **检查与使用之间的竞态。** `resolve()` 与随后的 `open()` 之间，符号链接可以被替换。严格的实现需要 `O_NOFOLLOW` / `openat` 一类按目录句柄逐级打开的方式。

这三条都说明：**真正的隔离只能来自操作系统层面的边界**，也就是沙箱。

## 漂移检测

Run 暂停或进程崩溃后，可能过了几个小时才恢复，这期间用户可能改过文件。模型脑子里的文件内容已经过期，如果不提醒，它会基于旧认知继续编辑。

`Harness._checkpoint()` 在检查点记录 `workspace.manifest()`——一张“相对路径 → [大小, mtime_ns]”的清单；恢复时 `_resume_note()` 重新生成清单并比较：

```python
@staticmethod
def diff_manifest(old: dict[str, list[int]], new: dict[str, list[int]]) -> ManifestDiff:
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    modified = sorted(k for k in set(old) & set(new) if old[k] != new[k])
    return ManifestDiff(added, removed, modified)
```

有变化时，恢复说明里会追加一句：“在此期间工作区发生了变化（修改：x.py）。继续之前，请重新读取与任务相关的文件。”

设计取舍：

- **用大小 + mtime，不算哈希。** 恢复时要扫描整个工作区，逐文件算哈希在大仓库里太慢；大小或 mtime 变了几乎总意味着内容变了。
  代价是“内容改了但大小和 mtime 都没变”这种极少见的情况检测不到。
- **最多 5000 个文件，跳过 `IGNORED_DIRS`。** `node_modules`、`.venv`、`build` 等目录文件多、变动频繁，又很少是任务关注点。
  超过上限后清单被截断，截断之外的变化检测不到——大仓库里它是抽样，不是保证。
- **只告知，不阻止。** 漂移说明作为恢复提示交给模型，由模型决定重新读哪些文件，harness 不替它做判断。

## 影子 git：快照与回滚

### 为什么不用用户自己的 .git

给 Agent 的每次写操作打快照，最自然的想法是在用户仓库里 commit。这样做的问题：

| 问题 | 说明 |
|---|---|
| 污染历史 | 每次写入一个 commit，用户的 `git log` 被淹没 |
| 破坏暂存区 | `git add -A` 会把用户刻意没暂存的修改也暂存 |
| 并非都是 git 仓库 | 工作区可能是普通目录 |
| 与用户操作冲突 | 用户同时在 rebase、切分支 |

`ShadowGit` 的做法是另建一个仓库，只借用工作区作为工作树：

```python
def _git(self, *args: str, check: bool = True) -> str:
    env = {
        **os.environ,
        "GIT_DIR": str(self.git_dir),
        "GIT_WORK_TREE": str(self.work_tree),
        "GIT_TERMINAL_PROMPT": "0",
    }
    cmd = ["git", "-c", "user.name=flywhl", "-c", "user.email=flywhl@localhost",
           "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false", *args]
    # ... subprocess.run，超时 60 秒
```

`GIT_DIR` 指向影子仓库（默认 `.flywhl/shadow.git`），`GIT_WORK_TREE` 是工作区本身，用户的 `.git` 完全不受影响。几个 `-c` 参数让快照不依赖用户的 git 配置：
不需要配置用户名、不触发 GPG 签名、不做换行符转换（转换会让快照与真实文件不一致）。
`_ensure()` 初始化时在 `info/exclude` 里写入 `IGNORED_DIRS`，影子仓库不收录 `.git`、`.flywhl`、`node_modules` 等目录。

### 为什么每次写操作前都打快照

```python
# ToolExecutor._run_one：权限与审批通过之后、执行之前
effect = tool.effect_for(args)
snapshot = None
if effect != Effect.READ and rc.config.snapshots and rc.workspace.shadow is not None:
    snapshot = await rc.workspace.shadow.snapshot(f"before {tool.name} {call.id}")
rc.store.step_started(rc.run_id, call.id, tool=tool.name, effect=str(effect),
                      idem_key=ctx.idempotency_key, input_hash=short_hash(args), snapshot=snapshot)
```

- **回滚粒度是一次工具调用。** 快照 sha 写进 step 记录和工具结果的 `details.snapshot`，UI 的工具卡片上显示“执行前快照”，
  检查器的“历史”页签里可以对任意一个快照点“回滚到此”（会先弹出确认）。
- **在执行前打，不在执行后打。** 回滚的语义是“撤销这次调用”，所以需要的是调用之前的状态。
- **只对非只读调用打。** 只读调用不改变文件，不需要快照；bash 的等级由命令分析器决定，`ls` 不会触发快照。
- **快照失败不阻断执行。** `snapshot()` 捕获所有异常返回 `None`，git 不可用时 `enabled=False` 直接跳过——快照是增强能力，不是硬依赖。
  代价是快照可能在你不知情的情况下缺失，所以 step 记录里 `snapshot` 为空的写操作是回滚不了的。

`git add -A` 借助 git 的 stat 缓存，未变化的文件不会重新哈希，所以除了第一次，快照的耗时与改动量而非仓库大小相关。

### 原子写：快照覆盖不到的“写到一半”

快照解决“写错了”，解决不了“没写完”：进程在写文件中途被杀（OOM、部署重启、取消宽限期后的 SIGKILL），文件停在半截，
下一次 Run 读到的是损坏的源码，漂移检测也只会报告“文件变了”。`write` 和 `edit` 都经过 `tools/fs.py::_atomic_write`：

```python
def _atomic_write(p: Path, text: str) -> None:
    """原子写入：写临时文件 → fsync → os.replace。进程在写一半时被杀，原文件要么是旧内容，要么是新内容。"""
    tmp = p.with_name(f".{p.name}.flywhl-tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    if p.exists():
        os.chmod(tmp, p.stat().st_mode)
    os.replace(tmp, p)
```

| 步骤 | 防的是什么 |
|---|---|
| 临时文件放在 **同一目录** | `os.replace` 只在同一文件系统内是原子的，放到 `/tmp` 可能跨设备 |
| `flush` + `fsync` 之后再替换 | 防止“改名已落盘、数据还在页缓存”，断电后得到一个空文件 |
| 复制原文件的 `st_mode` | 临时文件按 umask 创建，不复制会让可执行脚本丢掉 `x` 位 |
| `os.replace` | 原子替换目录项：任何时刻读到的要么是旧内容，要么是新内容 |

几个副作用要知道：替换的是目录项，所以硬链接会被“断开”（工作区里那份变成新文件，链接到的外部文件保持原样），
owner / group 与扩展属性不保留，只复制了权限位；目录本身没有 `fsync`，断电后可能回到旧内容，但不会是半截内容；
进程恰好死在替换之前，会留下一个 `.<文件名>.flywhl-tmp`。

### restore 如何处理新增文件

```python
async def restore(self, sha: str) -> list[str]:
    """把工作区回滚到快照 sha：恢复被修改/删除的文件，删除快照之后新增的文件。返回受影响文件。"""
    async with self._lock:
        def run() -> list[str]:
            self._git("add", "-A")
            changed = [x for x in self._git("diff", "--cached", "--name-only", sha).splitlines() if x]
            added = [x for x in self._git("diff", "--cached", "--name-only", "--diff-filter=A", sha).splitlines() if x]
            self._git("checkout", sha, "--", ".")
            for rel in added:
                p = self.work_tree / rel
                if p.is_file():
                    p.unlink()
            self._git("add", "-A")
            self._git("commit", "-q", "--allow-empty", "-m", f"restore {sha[:10]}")
            return changed
        return await asyncio.to_thread(run)
```

`git checkout <sha> -- .` 只会恢复快照里 **存在** 的文件（修改的改回去、删除的找回来），对快照之后新增的文件无能为力。
所以要先用 `--diff-filter=A` 找出“当前有、快照没有”的文件，checkout 之后逐个删除。
最后再提交一个 `restore` 记录——回滚本身也成了一个快照点，回滚错了还能再滚回来。

Web 端的入口是 `POST /api/sessions/{sid}/rollback`：会话有运行中的 Run 时返回 409，避免回滚与 Agent 的写入交错；
`GET /api/sessions/{sid}/snapshots` 列出快照历史。

### 局限

用一个小实验（新建 `.gitignore` 忽略 `.env`，打快照，然后修改 `x.py`、新建 `new/n.py`、修改 `.env`，再回滚）可以观察到：

| 情况 | 回滚后 | 原因 |
|---|---|---|
| 修改过的 `x.py` | 恢复 | 正常路径 |
| 新增的 `new/n.py` | 删除，但空目录 `new/` 还在 | git 不跟踪目录 |
| 被 `.gitignore` 忽略的 `.env` | **未恢复** | 工作区自己的 `.gitignore` 对影子仓库同样生效 |
| `node_modules`、`build` 等 | 不受影响 | 被 `info/exclude` 排除 |
| 子目录里另一个 git 仓库 | 只记录为嵌套仓库的引用 | git 不会递归收录嵌套仓库的内容 |
| 工作区外的副作用：数据库、远端 API、`~/.cache`、`git push` | 无法回滚 | 快照只覆盖工作区文件 |

最后一行最重要：**影子 git 回滚的是文件，不是世界。** 发出去的通知、推到远端的提交、写进数据库的行，需要的是幂等与对账（见 [14 幂等](/reliability/idempotency)），
不是快照。

### 一个 Harness、多个工作区：影子仓库按工作区分开

`Harness` 把解析后的 **绝对** 数据目录传给每个 `Workspace`，所以用 `create_session(workspace=...)` 指定了不同工作区的会话共用同一个数据目录。
早期版本在这个目录下只放一个 `shadow.git`：不同工作树共用一份 index 和提交历史，A 工作区的 `git add -A` 会把 B 的文件当成“已删除”，
对一个会话“回滚到”另一个工作区的快照，会把对方的文件检出到当前工作区，并删除当前工作区独有的文件。现在 `Workspace.__init__` 按位置选名字：

```python
# 一个 Harness 可以服务多个工作区（共用数据目录）：影子仓库必须按工作区分开，
# 否则不同工作树共用同一个 index，快照与回滚会互相覆盖
# （只有数据目录就在本工作区根下时才用固定名字；工作区是别的工作区的父目录时也要区分开）
own = self.data_dir.resolve().parent == self.root
git_dir = self.data_dir / ("shadow.git" if own else f"shadow-{short_hash(str(self.root))}.git")
self.shadow = ShadowGit(self.root, git_dir) if snapshots else None
```

| 数据目录位置 | 影子仓库 | 说明 |
|---|---|---|
| 直接位于工作区根下（默认 `<root>/.flywhl`） | `<root>/.flywhl/shadow.git` | 数据目录本身就专属于这个工作区，保持原名，已有快照不受影响 |
| 其它位置（多个工作区共用，或位于更深的子目录） | `<data_dir>/shadow-<hash>.git` | `<hash>` 是工作区绝对路径的 `short_hash`（SHA-1 前 16 位），每个工作区一份 |

用路径哈希而不是工作区名，是因为不同父目录下可以有同名的工作区；代价是工作区目录被移动或改名后会得到一个新的空影子仓库，旧快照留在原文件里。
判定条件是“数据目录的父目录 **就是** 工作区根”，而不是“数据目录位于工作区之内”：后者会让嵌套工作区撞车——
主工作区 `/a/b`、数据目录 `/a/b/.flywhl`，另一个会话的工作区是父目录 `/a` 时，数据目录同样位于 `/a` 之内，两者会选中同一个 `shadow.git`
（`tests/test_security.py::test_nested_workspace_does_not_share_shadow_repo`）。即便如此，嵌套的工作区依然会互相看到对方的文件，最好避免。
`tests/test_security.py::test_shadow_repo_is_per_workspace` 让两个工作区共用一个数据目录，断言两者的 `git_dir` 不同。

## 快照技术路线对比

影子 git 是最轻的一种快照。根据需要覆盖的范围，业界有几条路线：

| 路线 | 覆盖范围 | 速度与成本 | 可 diff | 主要问题 | 适用 |
|---|---|---|---|---|---|
| git 提交（含影子仓库） | 被跟踪的文件 | 增量、毫秒到秒级 | 是，可读性最好 | 不含被忽略文件、依赖目录、进程状态 | 本地编码 Agent 的逐步回滚 |
| overlayfs / 卷快照（如 btrfs、ZFS、云盘快照） | 整个文件系统，含未跟踪文件和依赖 | 写时复制，创建很快 | 只能文件级比较 | 依赖存储层能力，跨机迁移要复制卷 | 沙箱级“工作现场”保存与分叉 |
| CRIU 进程快照 | 进程内存、文件描述符 | 与内存大小相关 | 否 | 对内核版本、网络连接、GPU 状态敏感，脆弱 | 长时间运行的有状态进程，较少用于 Agent |
| microVM 快照 | 整台虚拟机：内存 + 磁盘 | 最重，但恢复可达百毫秒级 | 否 | 存储成本高，快照与宿主配置耦合 | 沙箱预热、整机分叉 |

选型的问题是“恢复时需要回到什么”：只需要代码回到某一步，git 足够；需要连同安装好的依赖、生成的缓存一起恢复，就需要卷快照；
需要连运行中的开发服务器一起恢复，才轮到 VM 快照。

## 多 Agent 并行：同一个仓库怎么分

本项目的子代理（[20 子代理](/multi-agent/subagents)）与父代理 **共享同一个工作区**：
`Harness` 为子代理创建会话时传入的是父工作区的根目录，隔离的是上下文，不是文件。
串行派发没有问题；并行派发多个会写文件的子代理时，就需要下面的方案之一。

### 每个 Agent 一个 worktree

`git worktree add ../task-42 -b agent/task-42` 让每个 Agent 拥有独立的目录和分支，共享同一个对象库，创建很快、不复制历史。
Agent 之间在开发阶段互不干扰，冲突被推迟到合并阶段，由 **合并队列** 裁决：

```mermaid
flowchart LR
    A1["Agent 1<br/>worktree + 分支"] --> Q
    A2["Agent 2<br/>worktree + 分支"] --> Q
    A3["Agent 3<br/>worktree + 分支"] --> Q
    Q["合并队列<br/>逐个 rebase 到最新主干"] --> T{"测试通过？"}
    T -->|"是"| M["合入主干"]
    T -->|"否 / 冲突"| R["退回对应 Agent<br/>附冲突与失败信息"]
```

合并队列的价值是把“谁先合、冲突了怎么办”从 Agent 之间的协商变成确定的流程：每次只合一个，合之前基于最新主干跑测试，
冲突或失败的任务带着具体信息退回给它的 Agent 去解决。

### 共享工作区的冲突处理

有些任务没法拆成独立分支（例如多个 Agent 协作调试同一个运行中的服务）。这时有三种思路：

| 方案 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| 文件锁 | 写之前申请锁，持有期间其他 Agent 只能读 | 实现简单，可以挂在 PreToolUse hook 上 | 锁粒度难定；Agent 崩溃会留下死锁，需要租约与超时 |
| 所有权划分 | 规划阶段把目录 / 模块分给不同 Agent，越界写被拒绝 | 没有运行时竞争，可以用权限规则实现 | 依赖任务能被干净地拆分；跨模块修改要走协调者 |
| 合并队列 | 各自在副本上改，统一排队合入 | 冲突处理有确定流程 | 需要副本（worktree / 卷克隆），延迟反馈 |

“所有权划分”在本项目里几乎不需要新代码：给每个子代理配置不同的规则，例如 `deny:write(*)` 加 `allow:write(src/api/*)`——
只是要记得 bash 通道同样需要约束，文件规则管不到 `sed -i`。

## 沙箱运行时对比

当 Agent 执行的是自己生成的代码，隔离强度应该按“这段代码完全不可信”来设计。主流运行时：

| 运行时 | 隔离机制 | 与宿主共享内核 | 启动 | 兼容性 | 采用情况（以各方官方资料为准） |
|---|---|---|---|---|---|
| 容器（runc） | namespaces + cgroups + seccomp | 是 | 最快 | 最好 | 普遍 |
| gVisor | 用户态内核拦截系统调用 | 否（Sentry 代为处理） | 快 | 部分系统调用不支持，I/O 密集负载有开销 | Modal、Kubernetes agent-sandbox 项目默认采用 |
| Firecracker microVM | 硬件虚拟化（KVM），极简设备模型 | 否 | 百毫秒级 | 需要构建 VM 镜像，设备支持有限 | E2B 等 |
| Kata Containers | 容器接口 + 每个 Pod 一个轻量 VM | 否 | 慢于容器 | 兼容 OCI / Kubernetes 生态 | 需要 VM 级隔离又想保留容器工作流的场景 |

选型可以按三个问题走：

1. **执行的代码是谁写的？** 只跑自己团队审过的脚本，容器足够；跑模型生成或用户提交的代码，尤其多租户共享宿主时，应选不共享内核的方案。
2. **需要多快、多密？** 每次对话都新建沙箱、并发很高时，启动时间和单实例开销会主导成本，gVisor 与 Firecracker 是常见折中。
3. **需要什么能力？** 需要 Docker-in-Docker、GPU、特殊设备时，兼容性会排除一部分选项。

Kubernetes 的 agent-sandbox 项目提供面向 Agent 的沙箱 CRD 与预热能力，具体特性以项目官方文档为准。

## 冷启动与预热池

沙箱的冷启动由几段串行的时间构成：

| 阶段 | 典型内容 | 优化手段 |
|---|---|---|
| 镜像拉取 | 下载基础镜像与语言运行时 | 节点上预拉取；精简镜像；按层缓存 |
| 运行时初始化 | 创建容器 / 启动 VM | 选用启动快的运行时；从内存快照恢复 VM |
| 依赖安装 | `npm install`、`pip install` | 把依赖烘进镜像；依赖目录卷快照复用 |
| 工作区恢复 | clone 仓库、恢复上次的现场 | 浅克隆；从卷快照克隆；增量同步 |

预热池把前两段（有时包括第三段）提前做完：维护一批已启动、未分配的沙箱，请求到达时直接领用。容量可以用 Little 定律估算——
**池容量 ≈ 峰值到达率 × 冷启动时长 + 余量**。例如高峰期每秒新开 5 个会话、冷启动 8 秒，池里至少要有 40 个实例，
否则高峰期的请求会退化成冷启动。预热实例要定期轮换：它们闲置期间同样会累积状态，镜像更新后也要替换。

## 出口管控与凭据代持

沙箱限制了 Agent 能碰到的文件，却不一定限制它能连到哪里。被注入的 Agent 最直接的破坏方式往往是外发：
把读到的代码或数据 `curl` 到攻击者的服务器。网络边界的常见做法：

- **默认拒绝 + 域名白名单。** 只放行包仓库、代码托管、必要的 API；其余连接一律拒绝。
- **出口代理审计。** 所有流量经过代理，记录目标与数据量，异常外发可告警。
- **DNS 控制。** 只允许使用受控解析器，阻止通过 DNS 查询本身外带数据。
- **阻止访问内网和云元数据地址。** 特别是 `169.254.169.254` 这类实例元数据端点，它们往往能直接换出云凭据。

凭据的原则是 **不进沙箱**。按调研资料，Anthropic Managed Agents 的做法是：git 令牌只在初始化克隆仓库时使用；
MCP 调用经过代理，由代理从 vault 取凭据，沙箱里的代码拿不到原始密钥。沙箱内的进程只能“请代理代为调用”，
即使整个沙箱被攻破，攻击者拿到的也只是一个受限、短期、可撤销的调用通道。

本项目的最小实现是 bash 子进程剥离密钥环境变量（`src/flywhl_harness/tools/shell.py::_clean_env`）：

```python
_SECRET_ENV = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)", re.I)

def _clean_env(strip: bool) -> dict[str, str]:
    env = dict(os.environ)
    if strip:
        env = {k: v for k, v in env.items() if not _SECRET_ENV.search(k)}
    env.setdefault("PAGER", "cat")
    env["GIT_PAGER"] = "cat"
    env["NO_COLOR"] = "1"
    return env
```

harness 进程自己需要 `ANTHROPIC_API_KEY` 调模型，但模型执行的 `env`、`printenv` 看不到它。这是按 **变量名** 剥离，边界很清楚：

| 情况 | 是否被剥离 |
|---|---|
| `OPENAI_API_KEY`、`GITHUB_TOKEN`、`AWS_SECRET_ACCESS_KEY` | 是 |
| `SSH_AUTH_SOCK` | 是（名字含 AUTH），副作用是 bash 里的 git 无法使用 ssh-agent |
| `DATABASE_URL=postgres://user:pass@host/db` | **否**，密码藏在值里 |
| `~/.config/gh/hosts.yml`、`~/.docker/config.json` 等文件里的凭据 | **否**，环境变量过滤管不到文件；这条通道靠命令分析：路径命中密钥线索（dangerous），又在工作区外，default 模式下要确认，但 bypass 模式下照样能读 |

MCP 服务器进程复用同一个函数：`MCPClient` 以 `{**_clean_env(True), **self.env}` 启动子进程，服务器拿不到 harness 进程里的密钥类变量，
需要的凭证必须在 `flywhl.toml` 该服务器的 `env` 里显式给出——第三方 MCP 服务器拿到什么，从配置里一眼可见。
进程外命令 hook 则仍继承完整的 `os.environ`，不经过这层过滤（见 [16 Hooks](/governance/hooks)）。
环境变量剥离只是“别把钥匙放在桌上”，不是“把钥匙锁起来”；后者需要沙箱加凭据代持。

## 浏览器自动化的隔离

通过 CDP、Playwright 或 computer use 操作浏览器的 Agent，面对的是另一组风险：浏览器里有登录态，页面内容由第三方控制，下载的文件可能是可执行的。
基本要求：

- **独立的浏览器配置文件，跑在沙箱里。** 不复用用户日常的配置文件——那里有所有网站的 cookie。
- **禁止访问内网。** 浏览器能访问的地址，等于被注入页面能让 Agent 访问的地址；内网管理后台、元数据端点都应屏蔽。
- **下载隔离。** 下载的文件落在沙箱内的专用目录，不自动打开、不进入工作区。
- **页面内容是不可信输入。** 页面文本、DOM 属性、截图里的文字都可能是注入。在本项目里，如果通过 MCP 接入浏览器工具，
  `tools/mcp.py` 按规范把未声明 `openWorldHint` 的工具视为开放世界、输出标为污点，后续副作用操作会升级为人审；
  要警惕的反而是服务器把浏览器类工具声明成 `openWorldHint: false`——注解由服务器自己填写，接入第三方服务器前应核对。

## 接入远程沙箱执行器

本项目没有 VM 或容器沙箱：bash 在宿主机上以工作区为 `cwd` 运行，`start_new_session=True` 只是为了能整组终止进程树，不是隔离。
有两条路可以补上这一层。

**整体放进容器。** 最简单：把 flywhl-harness 进程连同工作区一起跑在容器里，只挂载工作区目录，不挂载家目录，不传入除模型 API 密钥之外的凭据，
并限制容器的网络出口。这就是 pi 推荐的形态，适合本地开发。

**只把执行面放进远程沙箱。** harness 留在可信环境，bash 的执行转发给远程沙箱。做法是替换注册表里的 `bash` 工具：

```python
from flywhl_harness import Harness
from flywhl_harness.tools import BashTool, ToolResult, default_tools

class RemoteBashTool(BashTool):
    """保留 name="bash"：权限策略对 bash 的命令分析、复合命令规则、grant_for 都按工具名生效。"""

    def __init__(self, sandbox, **kw):
        super().__init__(**kw)
        self.sandbox = sandbox                     # 你的沙箱客户端（示意接口）

    async def execute(self, args, ctx):
        async for chunk in self.sandbox.exec_stream(   # 示意：远程执行并流式返回
                session=ctx.session_id, command=args["command"],
                timeout=args.get("timeout"), idempotency_key=ctx.idempotency_key):
            ctx.emit({"output": chunk.text})            # 沿用 tool_update 流式输出
            ctx.cancel.raise_if_cancelled()
        res = await self.sandbox.result(ctx.idempotency_key)
        return ToolResult(res.output + f"\n[退出码 {res.exit_code}]", is_error=res.exit_code != 0,
                          details={"exit_code": res.exit_code, "sandbox": res.sandbox_id})

tools = default_tools(cfg)
tools.register(RemoteBashTool(sandbox_client), replace=True)
h = Harness(cfg, provider=provider, tools=tools)
```

示意代码里的 `sandbox` 接口是假设的，替换为你使用的沙箱服务。真正落地时要处理三件事：

1. **文件工具要和 bash 看到同一份文件。** 只替换 bash 而 read / write / edit 仍操作本地目录，就会出现“写在本地、跑在远端”。
   要么让沙箱挂载同一个卷，要么把文件工具也替换成远程实现。
2. **快照位置随之迁移。** 影子 git 操作的是本地工作区；执行面在远端时，快照应改用沙箱侧的卷快照或在沙箱内运行影子 git。
3. **取消与幂等。** 远程执行的取消需要显式通知沙箱终止进程；`ctx.idempotency_key` 应传给沙箱，崩溃恢复时可以据此查询执行结果
   （实现 `reconcile()`，见 [14 幂等](/reliability/idempotency)）。

更完整的扩展方式见 [30 扩展指南](/advanced/extending)。

## 动手验证

路径约束、漂移检测与回滚的最小实验（在临时目录里运行，不会碰到你的项目）：

```python
import asyncio, os, tempfile
from pathlib import Path
from flywhl_harness.runtime.workspace import Workspace

async def main():
    root = Path(tempfile.mkdtemp())
    ws = Workspace(root)
    (root / ".gitignore").write_text(".env\n")
    (root / ".env").write_text("K=1")
    (root / "x.py").write_text("v1")
    s0 = await ws.shadow.snapshot("s0")

    m0 = ws.manifest()
    (root / "x.py").write_text("v2!")
    (root / "new").mkdir(); (root / "new" / "n.py").write_text("n")
    (root / ".env").write_text("K=2")
    print("漂移：", Workspace.diff_manifest(m0, ws.manifest()).describe())

    print("回滚影响：", await ws.shadow.restore(s0))
    print("x.py =", (root / "x.py").read_text(), "| new/ 仍存在：", (root / "new").exists(),
          "| .env =", (root / ".env").read_text())

    os.symlink("/etc", root / "etc_link")
    try:
        ws.resolve("etc_link/hosts")
    except Exception as e:
        print(type(e).__name__, e)

asyncio.run(main())
```

预期输出：漂移列出新增的 `new/n.py` 与修改的 `x.py`（`.env` 同样会出现，清单不受 `.gitignore` 影响）；
回滚后 `x.py` 恢复为 `v1`、`new/` 空目录仍在、`.env` 仍是 `K=2`；符号链接路径抛出 `PathEscape`。

验证密钥环境变量剥离：

```bash
FOO_API_KEY=x DATABASE_URL=postgres://u:p@h/db uv run python -c "
from flywhl_harness.tools.shell import _clean_env
e = _clean_env(True); print('FOO_API_KEY' in e, 'DATABASE_URL' in e)"
# 输出 False True：名字含 KEY 的被剥离，凭据藏在值里的没有
```

影子仓库按工作区分开（两个工作区共用一个数据目录时 `git_dir` 不同）：

```bash
uv run pytest tests/test_security.py::test_shadow_repo_is_per_workspace -q
```

在 Web UI 里：`flywhl serve -w playground`，让 Agent 创建并修改几个文件，打开检查器的“历史”页签，在快照列表里选一个点“回滚到此”，
确认文件回到了那一步之前的状态。

## 小结

- Workspace 是代码、依赖、执行环境、元数据组成的现场；本项目实现了创建、使用、快照、恢复，分叉与归档留给生产实现。
- 路径约束先 resolve 再比较，能识别 `../` 和符号链接逃逸；bash 只有按字面参数的近似检查，硬链接和竞态管不到——它是门，不是墙。
- 漂移检测用大小 + mtime 清单，在恢复时告诉模型哪些文件变了。
- 影子 git 不碰用户的 `.git`，按工作区分仓库，每次写操作前快照，回滚时删除新增文件并留下 restore 记录；它回滚的是工作区文件，不是外部世界。
- write / edit 原子写（临时文件 → fsync → `os.replace`），进程中途被杀也不会留下半截文件。
- 并行 Agent 用 worktree + 合并队列最稳妥；共享工作区要在文件锁、所有权划分、合并队列之间取舍。
- 沙箱按代码可信度、密度、能力需求在容器、gVisor、Firecracker、Kata 之间选型；冷启动靠预热池摊平，容量按到达率 × 冷启动时长估算。
- 出口默认拒绝，凭据由代理持有；本项目的环境变量剥离（bash 与 MCP 服务器共用）只是最小实现。

下一章把这些防线放到同一张威胁模型里：[19 安全：注入、凭据与供应链](/governance/security)。
