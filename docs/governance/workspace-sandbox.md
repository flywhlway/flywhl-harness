# 18 Workspace 与沙箱

前两章回答“这个动作能不能做”，本章回答另一半：**动作落在哪里，出了错能退回到哪里，最坏情况下能伤到什么。**
Workspace 是 Agent 的工作现场，沙箱是这个现场的围墙。flywhl-harness 实现了前者的路径约束、漂移检测和影子 git 快照；
后者默认开启：bash、命令 hook、MCP 服务器和验收命令都跑在 macOS sandbox-exec 或 Linux bubblewrap 里，网络默认受控。本章后半说明这两个档位的范围，以及怎样把 bash 接到远程沙箱上。

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
  （`cat ../../etc/hosts`）按 `normpath` 识别，花括号先展开再判断，以变量开头的路径（`D=/etc; cat $D/hosts`）按越界处理，
  命令里赋过值的变量按取值展开（`F=/etc/hosts; cat $F`），参数按真实路径再查一次凭证清单。
  但它终究只看命令文本：值要到运行时才知道的路径（`F=$(cat list.txt)`）、脚本与解释器里拼出来的路径都认不出，
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
def _git(self, *args: str, check: bool = True, errors: str = "strict") -> str:
    env = {
        **_clean_env(True),
        "GIT_DIR": str(self.git_dir),
        "GIT_WORK_TREE": str(self.work_tree),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    cmd = ["git", "-c", "user.name=flywhl", "-c", "user.email=flywhl@localhost", "-c", "core.autocrlf=false",
           "-c", "commit.gpgsign=false", "-c", "core.fsmonitor=false", "-c", f"core.hooksPath={os.devnull}", *args]
    # ... subprocess.run，超时 60 秒
```

`GIT_DIR` 指向影子仓库（默认 `.flywhl/shadow.git`），`GIT_WORK_TREE` 是工作区本身，用户的 `.git` 完全不受影响。几个 `-c` 参数让快照不依赖用户的 git 配置：
不需要配置用户名、不触发 GPG 签名、不做换行符转换（转换会让快照与真实文件不一致）。
`_ensure()` 初始化时在 `info/exclude` 里写入 `IGNORED_DIRS`，影子仓库不收录 `.git`、`.flywhl`、`node_modules` 等目录。

影子 git 跑在系统沙箱外，读写的却是沙箱里的命令写过的工作区，所以它不带密钥变量、不读全局配置、不跑 hook。
嵌套仓库要单独处理：`git add -A` 会把它收录成 gitlink，下一次 add 时 git 进到里面跑 `git status`，那个进程读的是嵌套仓库自己的配置，
`core.fsmonitor` 写什么就执行什么，`-c` 和 `GIT_CONFIG_*` 都传不进去。沙箱里的命令写得出这样的仓库，等于让 harness 替它在沙箱外执行命令。
`_add_all()` 在 add 前后都把 gitlink 从索引里删掉，git 就不会再进去，升级之前留下的 gitlink 也一并清掉。

### 为什么每次写操作前都打快照

```python
# ToolExecutor._run_one：权限与审批通过之后、执行之前
effect = tool.effect_in(args, self._root)
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
| 子目录里另一个 git 仓库 | 不动 | 不收录：留下 gitlink 的话，git 会进去执行它的配置 |
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
_SECRET_ENV = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASSPHRASE|CREDENTIAL|AUTH|COOKIE"
                         r"|_(PWD|PASS|PAT|DSN)(_|$)", re.I)   # _PWD 要带前缀：PWD / OLDPWD 是当前目录
_URL_WITH_PASSWORD = re.compile(r"^[a-z][a-z0-9+.-]*://[^/\s:@]*:[^/\s@]+@", re.I)

def _clean_env(strip: bool) -> dict[str, str]:
    env = dict(os.environ)
    if strip:   # GIT_CONFIG_KEY_<n> 是配置键名（名字里带 KEY），删掉它而留着 GIT_CONFIG_COUNT，git 会直接报错
        env = {k: v for k, v in env.items()
               if not ((_SECRET_ENV.search(k) and not _GIT_CONFIG_KEY.fullmatch(k)) or _URL_WITH_PASSWORD.match(v))}
    env.setdefault("PAGER", "cat")
    env["GIT_PAGER"] = "cat"
    env["NO_COLOR"] = "1"
    # …… 再用 GIT_CONFIG_* 追加命令行级配置：core.fsmonitor=false、safe.bareRepository=explicit
    return env
```

harness 进程自己需要 `ANTHROPIC_API_KEY` 调模型，但模型执行的 `env`、`printenv` 看不到它。剥离看 **变量名**，外加一种值的形态（带密码的 URL），边界很清楚：

| 情况 | 是否被剥离 |
|---|---|
| `OPENAI_API_KEY`、`GITHUB_TOKEN`、`AWS_SECRET_ACCESS_KEY`、`MYSQL_PWD` | 是 |
| `SSH_AUTH_SOCK` | 是（名字含 AUTH），副作用是 bash 里的 git 无法使用 ssh-agent |
| `DATABASE_URL=postgres://user:pass@host/db` | 是，值是带密码的 URL |
| `PGCONN="host=db password=…"`、JSON 配置里的密码 | **否**，名字和值的形态都认不出 |
| `~/.config/gh/hosts.yml`、`~/.docker/config.json` 等文件里的凭据 | **否**，环境变量过滤管不到文件；这条通道靠命令分析：命令里出现凭证位置时任何模式都拒绝，但运行时才拼出来的路径等混淆写法认不出，要靠下文的沙箱档位由内核拒绝 |

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
  `tools/mcp.py` 默认把所有 MCP 工具的输出标为污点，后续副作用操作会升级为人审。服务器把浏览器类工具声明成
  `openWorldHint: false` 也不会改变这一点：注解由服务器自己填写，只有配置里写明 `trust_annotations = true` 才采信。

## 默认开启：档位、网络与离开沙箱

没有任何配置时，bash、命令 hook、MCP 服务器和 `verify_command` 都跑在系统沙箱里：读不到凭证，写不了工作区之外，网络受控。
本节是这个默认值的设计记录，后面两节讲 macOS、Linux 各自的实现。

### 档位怎么定

`permissions.sandbox` 默认是 `auto`，启动时探测一次（`tools/sandbox.py::probe`）：

| 平台 | 探测方法 | 探测不到时的原因 |
|---|---|---|
| macOS | 用一份带拒绝规则的配置真跑一次 `sandbox-exec` | 外层已有沙箱，不能嵌套 |
| Linux | 找到 `bwrap`，带上实际运行时用的命名空间（pid、ipc、net）真跑一次 | 没装 bubblewrap；user namespace 被 AppArmor、seccomp 或容器的 `/proc` 遮蔽拦下，报错里有对应的修法 |
| 其他平台 | 不探测 | 没有可用的系统沙箱 |

探测一定要真跑。只放行一切的配置在外层沙箱里照样成功，看不出嵌套；只带 `--ro-bind` 的 bwrap 试跑能过的环境，到 `--unshare-net` 才失败，
那样第一条命令才露馅。

`flywhl doctor`、`chat` 和 `serve` 的启动信息、「环境与安全」页显示的都是实际生效的状态（`Confinement.status()`），不是配置里写的。

### 没有可用的沙箱时

**拒绝执行，给出修法，不退回沙箱外。** bash、`verify_command` 返回错误；命令 hook 按失败处理，`pre_tool_use` 的守卫默认阻断；
配置了 MCP 服务器时 harness 启动失败。

考虑过“退回沙箱外并打警告”。一台没装 bubblewrap 的机器上，用户以为有隔离，实际没有；无人值守的运行里警告没人看。
默认值的意义在于不需要记得去检查它，所以这里宁可第一条命令就失败。代价是这类机器要多一步：装上 bubblewrap，或者明确关闭。

关闭只有三个入口：用户配置 `~/.flywhl/config.toml` 里的 `sandbox = "off"`、`-c` 指定的文件、`--sandbox off`。
项目配置里写的会被丢弃并提示，**信任了也一样**：信任一个仓库的配置，是信它的 hook 和模型设置，不等于同意拆掉隔离。陌生仓库里的
`flywhl.toml` 不能靠一次回车换到沙箱外运行。

### 默认网络

**默认 `allowlist`，白名单为空。** 效果等于断网。和 `deny` 的差别在报错：代理知道被拦的是哪个主机，结果里能写清楚怎么放行，模型和人都不用猜。

```text
[出站白名单拦下了 pypi.org:443。确实需要的话把主机加进 permissions.network_allow，或者启动时加 --net-allow pypi.org]
```

| `sandbox` 实际档位 | `sandbox_network = "auto"` 的结果 |
|---|---|
| `macos`、`linux` | `allowlist` |
| 明确写了 `off` | `allow`，已有配置的行为不变 |
| 探测不到 | `allowlist`，反正拒绝执行 |

默认放开网络的话，被注入的内容可以把工作区里的东西发出去：沙箱挡住了读 `~/.ssh`，挡不住 `curl` 把源码传走。代价是 `pip install`、
`npm install`、`git clone` 第一次会失败，要把主机加进 `network_allow`。

### 四条通道共用一层

`tools/confine.py` 的 `Confinement` 把配置解析成实际生效的档位，起出站代理，生成启动参数。四条通道走同一段代码：

| 通道 | 为什么要进沙箱 | 可写范围 |
|---|---|---|
| bash | 模型直接控制的命令 | 工作区、临时目录 |
| 命令 hook | 反复运行的第三方脚本，项目配置可以带进来 | 同上，加自己的状态目录 |
| MCP 服务器 | 长期运行的第三方代码，`npx`、`uvx` 现拉现跑 | 同上，加自己的状态目录 |
| `verify_command` | 跑的是 Agent 刚改过的代码：放在沙箱外，写一个 `conftest.py` 就出去了 | 工作区、临时目录 |

hook 和 MCP 服务器各有一份状态目录（`~/.flywhl/sandbox/<类别>-<名字>`，环境变量 `FLYWHL_STATE_DIR` 指向它），`XDG_CACHE_HOME`、`npm_config_cache`、
`UV_CACHE_DIR` 也指到这里：家目录在沙箱里只读，缓存放不下的话 `npx` 连包都装不了。Linux 上它们的 `/tmp` 也互相分开。
工作区的读写权限和 bash 一样，这是它们干活需要的，也意味着一个被攻破的 MCP 服务器能改工作区。

单条 hook 或 MCP 服务器可以单独设置，没写的跟随全局：

```toml
[mcp_servers.github]
command = "npx"
args = ["-y", "@example/mcp-server@1.2.3"]
network_allow = ["registry.npmjs.org", "api.github.com"]

[[hooks]]
event = "post_tool_use"
command = "~/.flywhl/hooks/notify.sh"
sandbox_network = "allow"       # 这条 hook 要发通知

[mcp_servers.keychain-bridge]
command = "/usr/local/bin/keychain-mcp"
sandbox = "off"                 # 只在用户配置里有效，项目配置写的会被丢弃
```

单独设置是为了不把全局沙箱关掉：有一个 MCP 服务器要读 `~/.aws`，不该让 bash 也跟着出去。

### 工作区里只读的位置

工作区可写，但里面有几处之后会在沙箱外起作用：

| 位置 | 之后在哪里起作用 |
|---|---|
| `.flywhl/`、`flywhl.toml` | 下一次启动时读成配置 |
| 数据目录（`data_dir` 可以改到别处） | 影子仓库和会话库，harness 在沙箱外读写 |
| `.git/hooks`、`.git/config` | 用户的 `git commit`，或一条获准离开沙箱的 `git push` |

`.git` 只保护已经存在的，空工作区里 `git init` 照常能用。`.git` 目录本身也不能改名，否则挪开再建一个新的就绕过去了：
macOS 上拒绝写这个目录项，Linux 上把它挂成挂载点。里面的索引、对象、引用照常写，`git add`、`git commit` 不受影响。
代价是 `git config`、`git remote add`、`git push -u` 这类要改 `.git/config` 的命令在沙箱里会失败，要走 `outside_sandbox`，或者自己在终端里跑。

工作区放在数据目录里面时不保护数据目录，否则整个工作区都成了只读。这种布局要自己留意。

硬链接绕不过这些位置，大小写变体也绕不过，都有真实沙箱用例：macOS 上 `ln` 本身被拒绝，Linux 上这些位置是单独的挂载，跨挂载链不了。

### 离开沙箱

确实需要在沙箱外跑一条命令时（`git push`、`gh`、`docker`），模型在 bash 调用里带 `outside_sandbox: true`。这是一道独立的人工门，
和审批不是一回事：

| 情况 | 结果 |
|---|---|
| default、accept_edits、bypass 模式 | 都要人确认 |
| 有 `allow` 规则、会话授权，或命中 `auto_approve` | 照样要人确认，这些都放行不了 |
| plan 模式、`deny` 规则、凭证位置、灾难性命令 | 拒绝，人工门不能把拒绝变成放行 |
| 无人值守（`on_ask` 不是 `wait`） | 拒绝，`--yes` 也不行 |
| 审批卡片、终端提示 | 写明“将在沙箱外执行”，风险记为 critical，没有“本会话始终允许” |
| 人点了批准并勾选“记住” | 只放行这一次，不产生授权 |
| hook 在审批之前把参数改成沙箱外 | 重新判定，走人工门，hook 自己放行不了 |
| 审批人改参数，或批准之后的 hook 加上 `outside_sandbox` | 拒绝：这次批准看的不是这条命令 |
| 批准之后 hook 换了命令 | 拒绝：人批准的是审批卡上那一条 |
| 回放、脚本里直接调用工具 | 拒绝：走不到审批，工具不放行 |
| 没配沙箱（`off`） | 没有门：本来就在沙箱外 |

实现上，工具用 `Tool.human_gate()` 声明这道门，权限判定把它记成 `PermissionResult.human_only`，放在模式、规则、授权之上，
执行器看到这个标记就不走 `auto_approve` 和无人值守分支。批准之后的复查和改写后的重新判定都看同一个标记，
所以 hook 也不能在批准之后把参数改成沙箱外。

工具自己还要再核对一次。执行器只在人批准之后才设 `ToolContext.human_approved`，`BashTool` 只认这个标记，只看参数不够：
回放把工具包成 `RecordedTool`，权限层看不到人工门，模式又是 bypass，没有录制结果的调用落到真实工具上时，`outside_sandbox` 就直接生效了。
这样任何走不到审批的路径，默认都是拒绝。

用户选择了让命令在外面跑，后果也在外面：它能读凭证、能写工作区之外，网络不受限。环境变量照样剥离。

### 已有配置的迁移

| 原来的配置 | 现在 | 要保持原样 |
|---|---|---|
| 什么都没写 | `auto`，网络 `allowlist` | 用户配置写 `sandbox = "off"` |
| `sandbox = "off"` 在用户配置里 | 不变 | 不用改 |
| `sandbox = "off"` 在项目配置里 | 丢弃并提示 | 挪到用户配置 |
| `sandbox = "macos"` 或 `"linux"`，没写网络 | 网络从 `allow` 变成 `allowlist` | 写 `sandbox_network = "allow"` |
| hook、MCP 服务器要读家目录里的文件或联网 | 会失败 | 按上面单独设置 |

### 开销

每条命令多出来的耗时，含生成配置、起出站代理、遍历工作区找凭证。工作区 2000 个文件，同一条 `true` 跑 200 次（`scripts/sandbox_overhead.py`），单位毫秒：

| 网络策略 | macOS p50 / p95 | Linux p50 / p95 |
|---|---|---|
| 沙箱外 | 5.0 / 8.4 | 0.2 / 0.3 |
| `allow` | 12.3 / 23.9 | 13.6 / 32.1 |
| `deny` | 11.3 / 13.2 | 14.4 / 53.0 |
| `allowlist` | 13.3 / 21.2 | 20.9 / 25.6 |

macOS 是 Apple Silicon 本机，Linux 是同一台机器上 Docker 里的 arm64 容器，不能当裸机数据。每条命令多十几到二十几毫秒，和模型调用的秒级相比可以忽略，
所以每条命令一个沙箱、一个代理是合适的。Linux 的准备时间随工作区增长：每条命令都要遍历工作区找凭证，超过 5 万个条目拒绝执行。

### 这一层不管的事

- **写进工作区、之后在沙箱外执行的东西。** `.git/hooks`、`.git/config` 已经只读，但沙箱里还能写 `Makefile`、`.envrc`、`package.json` 的脚本，
  以及嵌套仓库和子模块的配置（`.git/modules`）；工作区一开始没有 `.git` 时，第一条命令建出来的也没有保护。
  用户之后在终端里跑 `make`、进目录，或者批准一条 `outside_sandbox` 的命令，这些内容就在外面执行了。人工门上要看清楚命令本身，也要想到它会读工作区；
- **工作区本身的数据。** 工作区可读，白名单里的主机是出得去的。放行一个谁都能上传的主机，等于放行外发；
- **共享内核。** 系统沙箱挡的是误操作和注入驱动的越界，不是内核漏洞。跑不可信代码、尤其多租户时，见上文的运行时选型；
- **Linux 上工作区特别大时。** 见开销一节，这时拒绝执行而不是少查；
- **信任之后的配置。** `unix_sockets` 里写 `docker.sock`、`workspace = "/"` 这类配置，过了信任确认就按写的生效。确认提示里都列着，要看清楚再点；
- **放在 `/tmp` 下的命令。** Linux 沙箱换掉了 `/tmp`，那里的 venv 和解释器看不到，MCP 服务器会一启动就退出。`cwd` 单独挂回来了，命令没有；
- **没验证过的发行版。** NixOS 这类把命令放在 `/run/current-system` 下的系统没有测过：`/run` 在沙箱里是空的，`PATH` 里的命令可能找不到。

## macOS sandbox-exec 档位

macOS 上 `auto` 落到这一档，也可以写 `permissions.sandbox = "macos"`（或 `flywhl chat --sandbox macos`）。每条命令跑在 `sandbox-exec` 里（`tools/sandbox.py`）：
从 `(allow default)` 起步，收紧文件访问与本机进程间通信，网络默认只能经代理连白名单里的主机。生成的配置大致是：

```scheme
(version 1)
(allow default)
; 凭证位置：CREDENTIAL_GLOBS 逐条转成正则，外加 $KUBECONFIG、$GNUPGHOME 等环境变量指向的位置
(deny file-read* file-write* (regex #"^.*/\.ssh/.*$") (regex #"^.*/\.kube/.*$") ...)
; 写操作只能落在工作区、临时目录和几个设备文件上
(deny file-write* (require-not (require-any (subpath "<工作区>") (subpath "<临时目录>") (literal "/dev/null") ...)))
; harness 自己的状态与配置，与托管基线一致
(deny file-write* (subpath "<工作区>/.flywhl") (literal "<工作区>/flywhl.toml"))
; 本机 IPC：钥匙串、剪贴板、LaunchServices / Apple 事件，以及 launchd 任务
(deny mach-lookup (global-name "com.apple.SecurityServer") (global-name "com.apple.securityd.xpc") ...
                  (global-name "com.apple.pasteboard.1") (global-name-regex #"^com\.apple\.coreservices\."))
(deny job-creation)
; Unix 套接字任何网络策略下都先整体拒绝
(deny network-outbound (remote unix-socket))
; sandbox_network = "deny" / "allowlist" 时追加：对外 IP 连接（回环地址上的 TCP 也算）一律拒绝
(deny network-outbound (remote ip))
; 放行必须写在拒绝之后，否则不生效："allowlist" 放行本机出站代理的端口；
; "allow" 放行 DNS（mDNSResponder）与系统日志的套接字；unix_sockets 里列出的任何档位都放行
(allow network-outbound (remote ip "localhost:<代理端口>"))
(allow network-outbound (remote unix-socket (path-literal "/private/var/run/mDNSResponder")))
```

凭证清单与文件工具、命令分析共用 `policy/sensitive.py` 那一份，区别在于内核按 **真实路径** 判定，不管命令怎么写：

| 动作 | 命令分析（字符串） | sandbox-exec（内核） |
|---|---|---|
| `cat ~/.kube/config` | deny | 拒绝 |
| `D=$(echo $HOME/.ku); cat ${D}be/config`（运行时才拼出来）、glob、编码过的路径 | 认不出，bypass 下放行 | 拒绝 |
| 解释器里拼出路径再读（`python3 -c "..."`） | 无法静态分析，bypass 下放行 | 拒绝 |
| 写工作区外（`echo x >> ~/.zshrc`） | external，bypass 下放行 | 拒绝（临时目录除外） |
| 改 `.flywhl/`、`flywhl.toml` | 托管 ask | 拒绝 |
| 网络外发（`curl`、`nc`、DNS 查询） | — | 默认只能经出站代理连白名单里的主机，白名单默认为空；`deny` 时全部拒绝；`allow` 时不限制 |
| 连本机守护进程（`docker.sock`、tmux、ssh-agent） | — | 拒绝，`unix_sockets` 列出的除外 |
| 钥匙串（`security`、`git credential-osxkeychain`）、剪贴板 | 导出口令的 `security` 子命令 deny | 拒绝 |
| 让沙箱外的进程替它执行（`open`、`osascript`、`launchctl`） | `launchctl` 判为高危 | 拒绝 |
| 回环地址上的本机 TCP 服务 | — | 拒绝，`network_allow` 里写了 `localhost:端口` 的除外；`allow` 档**不限制** |
| 其余 Mach 服务 | — | **不限制** |
| 命令 hook、MCP 服务器、`verify_command` | — | 同样的规则，见[四条通道](#四条通道共用一层) |

`tests/test_security.py::test_sandbox_blocks_credential_reads_static_analysis_misses` 在 bypass 模式下把同一条运行时拼出凭证路径的命令跑两遍：
沙箱关闭时读到了测试用的假凭证，打开后读不到，工作区照常可写。`test_macos_sandbox_confines_writes` 核对写操作的边界，`tests/test_hardening.py::test_sandbox_network_deny_blocks_outbound_connections` 核对断网。

代价与边界：

- **要用凭证的命令会失败。** `ssh`、`git push`（SSH 协议）、`kubectl`、`gh`、`docker login` 读不到自己的凭证文件——
  这一档就是拿这些能力换“读不到”（bash 本来也剥离了 `SSH_AUTH_SOCK`）。`docker` 整个用不了：放行 `docker.sock`
  就等于放弃沙箱。
- **`allow` 档只管 Unix 套接字，不管回环地址上的 TCP 服务。** 没开认证的本机服务（Jupyter、数据库、调试端口）照样连得上；
  要连这些一并拦住，用 `deny` 或 `allowlist`。
- **写全局缓存的工具可能失败或退化。** 默认只放开工作区和临时目录，`~/.cache`、`~/Library/Caches`、`~/.npm` 写不进去；
  bash 没有可配置的白名单（`sandbox_argv()` 的 `writable` 参数留了口子），hook 与 MCP 服务器的缓存转到各自的状态目录。
- **fail-closed。** sandbox-exec 不可用时（文件被删、外层已有沙箱），命令不执行、返回错误，不会退回沙箱外运行
  （`test_sandbox_fails_closed_when_unavailable`、`test_auto_without_any_sandbox_refuses_and_says_how_to_fix_it`）。
- **sandbox-exec 已被 Apple 标为 deprecated**，但仍随系统提供；配置语法（SBPL）没有正式文档，系统升级后要重跑上面的测试。
  非 macOS 或 sandbox-exec 不能用时，相关测试自动跳过。
- 项目配置可以打开它，不能关掉（`sandbox = "off"` 信任了也丢弃）。`sandbox_network` 可以设成 `deny`；`allow`、`allowlist`、`auto` 都要先信任，
  因为不知道用户配置里原来是哪一档，它们相对 `deny` 都是放宽。`network_allow` 的条目、`unix_sockets` 放行的套接字同样要信任，见 [19 安全](/governance/security)。

### 本机 IPC：沙箱外的进程替它执行

文件规则只管这个进程自己读写什么。可它还能请本机别的进程代劳，那些进程不在沙箱里：

- `docker.sock`：`docker run -v ~:/h alpine rm -rf /h`，删的是真实的家目录，写入边界形同虚设；
- tmux、screen 的套接字：往一个开着的终端里敲命令；ssh-agent 的套接字：不读私钥文件也能用私钥登录；
- LaunchServices 与 Apple 事件：`open` 拉起任意程序，`osascript` 让终端执行命令；launchd：`launchctl` 提交后台任务；
- 钥匙串与剪贴板：`git credential-osxkeychain get` 吐出 GitHub 令牌，`pbpaste` 读到用户刚复制的口令。

这些通道都在实机上做过对照实验：探针只在临时目录里 touch 一个标记文件，钥匙串只查锁定设置、不碰口令。

| 通道 | 不加限制时 | 加了规则之后 |
|---|---|---|
| `docker.sock` | 连得上守护进程 | `permission denied` |
| ssh-agent | 列出已加载的私钥 | `Operation not permitted` |
| `open` 拉起探针程序 | 标记文件出现，程序在沙箱外运行 | 拉不起来；`coreservices` 下的服务单独拒绝哪一个都还有退路，只能整组拒绝 |
| `launchctl submit` / `bootstrap` | 沙箱外执行成功 | 沙箱里本来就被 launchd 拒绝，`(deny job-creation)` 写明而不依赖它 |
| 钥匙串、剪贴板 | 读得到 | 读不到 |

规则加上之后，DNS 解析、`curl` / `git` / Python / Node 的 HTTPS、`xcrun`、`brew`、`osascript` 本地计算照常可用：
TLS 证书校验走 `trustd`，不受影响。`tests/test_backlog.py` 核对 Unix 套接字（三种网络策略，含 `unix_sockets` 放行）与钥匙串。

本机数据库常用 Unix 套接字连接。沙箱里要用的，逐个列出：

```toml
[permissions]
unix_sockets = ["/var/run/postgresql/.s.PGSQL.5432"]
```

列出的套接字任何网络策略下都放行，所以它和白名单条目一样，写在项目配置里要先信任。放行位置有两条限制：

- **启动时定下，之后不再解析。** 目录部分解析符号链接，最后一级保留原样。每条命令都重新解析的话，命令可以先把那个位置换成指向 `docker.sock` 的符号链接。内核按解析后的真实路径判定连接（实测），放行位置上被换成的符号链接连不过去；
- **不能在沙箱可写的目录里**（工作区、临时目录），否则拒绝执行。符号链接挡得住，换目录挡不住：命令把上层目录整个换掉，路径照样对得上，连到的已经不是原来的服务。
  硬链接到工作区外的普通文件实测建不出来，套接字没有单独测过，这里不依赖它。

Homebrew 的 PostgreSQL 默认把套接字放在 `/tmp`，不能直接放行：把 `unix_socket_directories` 改到临时目录之外，或者改用本机 TCP 端口（`allowlist` 档写 `localhost:5432`）。

### 出站白名单

整体断网太粗：装依赖、拉代码都要联网，于是只能整体放开，被注入的内容驱动外发时就没有最后一道防线。
`allowlist` 是默认的网络策略，只放行 `network_allow` 里的主机：

```toml
[permissions]
network_allow = ["pypi.org", "files.pythonhosted.org", "*.githubusercontent.com", "localhost:5432"]
```

内核的规则写不了主机名：Seatbelt 只认 `localhost` 或 `*` 加端口，Linux 的网络命名空间更是只有通或不通。所以分两层（`tools/egress.py`）：

1. **内核层**：对外连接一律拒绝，只留一条到出站代理的路。Unix 套接字同样拒绝，因为 DNS 查询走 mDNSResponder 的套接字，
   数据可以编码进域名带出去；`docker.sock` 能起一个带网络的容器。这两条在 `deny` 档同样生效；
2. **代理层**：每条命令启动一个代理，只监听 127.0.0.1 的临时端口（Linux 上是 0700 目录里的 Unix 套接字），命令的 `HTTPS_PROXY` 等变量指向它。HTTPS 走 `CONNECT` 隧道，
   代理只看主机名和端口、不解密；主机不在白名单里就回 403，并在命令结果末尾注明被拦下的主机。主机名由代理解析，沙箱里不需要 DNS。

代理跑在沙箱外，它能连到的地方比沙箱多，所以还有三条规则：

- **解析出的地址再核一遍。** 被放行的域名解析到 127.0.0.1 或云元数据地址（169.254.169.254），就等于借代理绕过了内核层。
  回环地址只认 `localhost:端口` 这类本机规则，链路本地与保留地址一律不连，内网地址只认写全名的规则（通配子域名里可能有别人控制的名字）；
  连接用的就是核过的 IP，不再按名字解析第二次，DNS 重绑定换不了目标。TUN 模式代理软件用的 fake-ip 网段（198.18.0.0/15）按公网处理；
- **纯 HTTP 的 Host 头换成判定过的主机。** 否则连的是放行的服务器，请求的却是同一台机器上的别的站点；
- **命令结束时清理它的进程组。** 代理随命令关闭，沙箱放行的那个端口号之后可能被别的服务占用，留在后台的进程会连到占用者那里。
  自己另起会话（`setsid`）的进程不在组里，管不到。

白名单写法：`pypi.org` 只匹配这个主机的 80、443 端口；`*.github.com` 匹配子域名、不含 `github.com` 本身；
`registry.local:5000`、`example.com:*` 限定或放开端口；`localhost:5432` 这类本机端口连不支持 HTTP 代理的客户端也能用：macOS 在内核层直接放行，Linux 由转发器透明转发。
写法不对在启动时报错。命令行可以临时指定：`flywhl run --net-allow pypi.org --net-allow files.pythonhosted.org "装依赖并跑测试"`。

边界：

- 不走代理的连接都连不出去：SSH（包括 `git@github.com:` 和被 `insteadOf` 改写成 SSH 的地址）、`git://`、不读代理环境变量的程序。
  失败是预期的，不会绕过白名单；
- 代理不解密 TLS，只按 `CONNECT` 的主机名判定。同一 CDN 上可以借域前置访问别的站点（TLS 里的 Host 与外层主机名不同，
  检查 SNI 也拦不住），所以白名单只放必要的主机；
- 代理自己直连目标，不经过环境里的上游 HTTP 代理，必须经企业代理出网的环境暂不支持；
- 网络策略离不开沙箱：配置了 `deny` 或 `allowlist` 却没开沙箱时，bash 拒绝执行并说明原因，不会悄悄连外网；设置页同样标红。

`tests/test_backlog.py` 里的出站白名单用例在真实 Seatbelt 下核对：白名单内经代理和 `CONNECT` 可达、白名单外回 403、
绕过代理直连本机端口被拒、直连外部地址被拒、Unix 套接字被拒、命令结束后后台进程被清理；另有不经沙箱的用例核对地址复核与 Host 头重写。
`tests/test_sandbox_default.py::test_allowlisted_host_is_reachable_and_others_are_not` 在两个平台上都跑。

### Linux：bubblewrap 档位

`sandbox = "linux"` 用 bubblewrap（`bwrap`）实现同样的边界，`bwrap_argv()` 生成的参数大致是：

```text
bwrap --ro-bind / / --dev /dev --proc /proc --unshare-pid --unshare-ipc --die-with-parent --new-session
      --tmpfs /run                                   # docker.sock、D-Bus、XDG_RUNTIME_DIR 里的套接字都不出现
      [--unshare-net]                                # sandbox_network = "deny" 或 "allowlist"
      --bind <本会话私有目录> /tmp --tmpfs /var/tmp     # X11、tmux、ssh-agent 的套接字不出现；/tmp 在命令之间照样共用
      --bind <工作区> <工作区>                          # 可写：工作区
      --ro-bind <工作区>/.flywhl <工作区>/.flywhl     # harness 状态与项目配置再只读挂回去
      --tmpfs ~/.ssh --ro-bind /dev/null <工作区>/sub/.netrc ...  # 凭证位置：目录换成空目录，文件换成空文件
      --ro-bind /dev/null ~/.docker/run/docker.sock ...  # 其余看得见的宿主套接字（来自 /proc/net/unix）
      --bind /run/postgresql/.s.PGSQL.5432 /var/run/postgresql/.s.PGSQL.5432   # unix_sockets 列出的挂回来
      [--bind <0700 目录>/proxy.sock /run/flywhl-egress.sock]   # allowlist：宿主上出站代理的套接字
      --setenv TMPDIR /tmp --chdir <工作区> [python -I -S -c <转发器> ...] /bin/bash -c '<命令>'
```

`allowlist` 比 macOS 多一个问题：网络命名空间里只有回环，代理又在宿主上。沙箱里没有 IP 能连到宿主，所以代理改听 Unix 套接字，挂进沙箱；
程序只会连 TCP，所以沙箱里再起一个转发器，用标准库写成，`-I -S` 启动不碰工作区里的任何东西（`tools/sandbox.py::_RELAY`）：

- 听 `127.0.0.1:17890`，命令的 `HTTPS_PROXY` 指向这里；`network_allow` 里的每个 `localhost:端口` 也听上，收到连接就发一个 `CONNECT` 给代理，给不支持代理的客户端用；
- 它不做判断。放不放行全在宿主上的代理，转发器被沙箱里的命令杀掉或顶替，也只是连不出去；
- 先绑定端口再 fork：父进程 `exec` 成命令本身，退出码和信号都是命令的，子进程关掉标准输出再转发，否则 harness 等不到输出的 EOF。它随 PID 命名空间一起消失，不用清理；
- 解释器取 `sys.executable` 的真实位置。沙箱里 `/tmp` 是空的，装在那里的虚拟环境看不到，这时用系统的 `python3`，都没有就拒绝执行。

和 macOS 档的差别都来自 bubblewrap 只能按路径挂载、不能按模式拒绝：

| 方面 | macOS（Seatbelt） | Linux（bubblewrap） |
|---|---|---|
| 凭证位置 | 按清单正则拒绝，任何路径都算 | 家目录按清单查，工作区逐层遍历（超过 5 万个条目拒绝执行）；`node_modules`、`.venv`、`.git` 这类依赖与缓存目录不进去——每条命令都走一遍太慢，里面也不是用户放凭证的地方，放在那里的凭证认不出。工作区与家目录之外散落的私钥文件同样认不出 |
| 其它进程的环境变量 | 清单里的 `/proc/*/environ` | 新 PID 命名空间，看不到别的进程 |
| 新建 `flywhl.toml` | 拒绝 | 已存在的只读；还不存在的拦不住新建，新建的项目配置仍要经过信任确认 |
| Unix 套接字 | 按连接拒绝，`unix_sockets` 列出的放行 | 只读挂载挡不住连接，只能让它们不出现：`/run`、`/var/run`、`/var/tmp` 换成空目录，`/tmp` 换成本会话私有的目录（进程退出时删除）；其余位置上的（包括挂回 `/tmp` 下面的工作区）按 `/proc/net/unix` 逐个盖成 `/dev/null`。那里记的是绑定时写的路径：经符号链接绑定的按真实位置遮住，绑定后改过名的按原目录找出来。列出的再挂回来，只挂真的套接字。认不出的：按相对路径绑定的、所在目录被整个挪走的、被宿主进程硬链接到别处的、命令开始之后才出现的；`allow` 档与宿主共用网络命名空间，抽象命名空间里的套接字（部分 X11、D-Bus）仍然连得上 |
| 钥匙串、`open` 一类 | 拒绝对应的 Mach 服务 | 没有对应物；桌面会话的 D-Bus 在 `/run` 里，一并遮住 |
| `deny` 网络 | 拒绝 IP 连接与 Unix 套接字 | 新网络命名空间，抽象套接字也隔开了 |
| `allowlist` 网络 | 代理听回环端口，内核放行这一个端口 | 新网络命名空间，代理听 Unix 套接字，沙箱里的转发器接过来；`localhost:端口` 由转发器透明转发 |

没有 bubblewrap、或它因为用户命名空间被禁用而跑不起来时，命令同样拒绝执行。Ubuntu 24.04 起 AppArmor 默认不让无特权进程建用户命名空间，
要运行 `sudo sysctl kernel.apparmor_restrict_unprivileged_userns=0`，或者给 `bwrap` 单独配一个 AppArmor profile。在 Docker 容器里使用要放开两处默认限制：
seccomp 会拦下 bubblewrap 需要的 `unshare`（`--security-opt seccomp=unconfined`）；`/proc` 部分路径被遮住，挂新的 `/proc`
会失败（`--security-opt systempaths=unconfined`）。不想放开的话，把容器本身当作沙箱，在用户配置里写 `sandbox = "off"`。
`tests/test_backlog.py` 的 `needs_bwrap` 用例核对凭证遮蔽（含工作区子目录）、`.flywhl/` 只读、写入边界、宿主套接字不可见与断网，
它们只在有可用 bubblewrap 的 Linux 上运行。
这些用例和外层 bwrap 沙箱下的全量测试在 `python:3.12-slim` 一次性容器里跑过（不挂载宿主目录，代码用 tar 流传进去，普通用户运行），CI 上 Ubuntu 作业也跑，沙箱起不来时作业失败。
`scripts/sandbox_acceptance.py` 用干净安装的 wheel、不写配置，在两个平台上验证同一组边界：家目录放在 `/tmp` 之外，并放一份普通文件做对照，因为 Linux 沙箱的 `/tmp` 是私有的，凭证放在那里读不到说明不了什么。

### 设计草案：只在沙箱档位下允许 bypass（未实现）

bypass 的注释一直写着“只应在隔离沙箱中使用”，但没有任何机制检查这一点。打算这样补：

1. `permissions.sandbox` 增加取值 `external`：使用者声明 harness 已经跑在容器或 VM 里，隔离由外部负责。harness 验证不了这个声明，
   作用是让“我在沙箱外开 bypass”不再是默认值，而是一个写进配置、留在审计里的显式选择；
2. 构造 Run 时（`Harness._build_rc`）mode 为 bypass 而 sandbox 为 `off`，**拒绝启动** 并说明原因，而不是悄悄降级成 default——
   无人值守的任务降级之后会卡在审批上，比报错更难排查；
3. Web UI 与 `/mode bypass` 在 sandbox 为 `off` 时给出同样的拒绝。

没有直接实现，是因为它改变现有的公开行为：`examples/` 默认以 bypass 运行，CI 的 Ubuntu job 会跑这些示例；
回放（`observe/replay.py`）也以 bypass 创建会话；测试里大量使用 bypass。迁移要把这些位置显式声明为 `external`
（它们用脚本化模型，不需要真实隔离），这是对 v0.1.0 的不兼容改动，留待单独决定。

## 接入远程沙箱执行器

除了上面两个系统沙箱档位，本项目没有 VM 或容器沙箱，命令和 harness 共用一个内核。有两条路可以补上这一层。

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
- 路径约束先 resolve 再比较，能识别 `../` 和符号链接逃逸，文件工具执行前还按真实路径复查凭证清单；bash 按字面参数做近似检查（工作区里的链接按目标判定），硬链接管不到——它是门，不是墙。
- 漂移检测用大小 + mtime 清单，在恢复时告诉模型哪些文件变了。
- 影子 git 不碰用户的 `.git`，按工作区分仓库，每次写操作前快照，回滚时删除新增文件并留下 restore 记录；它回滚的是工作区文件，不是外部世界。
- write / edit 原子写（临时文件 → fsync → `os.replace`），进程中途被杀也不会留下半截文件。
- 并行 Agent 用 worktree + 合并队列最稳妥；共享工作区要在文件锁、所有权划分、合并队列之间取舍。
- 沙箱按代码可信度、密度、能力需求在容器、gVisor、Firecracker、Kata 之间选型；冷启动靠预热池摊平，容量按到达率 × 冷启动时长估算。
- 系统沙箱默认开启（`sandbox = "auto"`），用不了时拒绝执行，不退回沙箱外；只有用户配置、`-c`、`--sandbox off` 能关掉，项目配置不能。
  bash、命令 hook、MCP 服务器、`verify_command` 共用一层：凭证位置不可读写、写操作限制在工作区与临时目录。
- 网络默认是空白名单的 `allowlist`：经代理按主机名放行，DNS 与本机套接字一并拒绝，被拦时报错写明主机和放行办法。Linux 上代理经 Unix 套接字和沙箱里的转发器接起来。
- 要在沙箱外跑命令用 `outside_sandbox`，这是一道独立的人工门，任何模式都要人确认，`auto_approve`、规则、会话授权都放行不了，无人值守时拒绝。
  工具只认执行器在人批准之后给的标记，回放和直接调用走不到这里。
- 工作区里之后会在沙箱外起作用的位置只读：`.flywhl/`、`flywhl.toml`、数据目录、`.git/hooks`、`.git/config`。影子 git 自己也跑在沙箱外，不收录嵌套仓库，不执行它们的配置。
- “只在沙箱档位下允许 bypass”还是设计草案。
- 出口默认拒绝，凭据由代理持有；本项目的环境变量剥离（bash、MCP 服务器、命令 hook 共用一套规则）只是最小实现。

下一章把这些防线放到同一张威胁模型里：[19 安全：注入、凭据与供应链](/governance/security)。
