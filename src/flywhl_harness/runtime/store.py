"""持久化层：一个 SQLite 文件承载 Session / Run / Step / Event 四层模型。

四层模型（粒度递减）
--------------------
========  ==========================================  =================================
层         存什么                                       用途
========  ==========================================  =================================
Session   会话容器：工作区、标题、当前分支头（head）      多轮会话、分支（树）、隔离边界
Entry     会话中的条目（消息 / 压缩摘要 / 标签），树结构    上下文的“事实源”，只追加
Run       一次任务执行：状态、版本号、用量、心跳          生命周期状态机、计费、恢复锚点
Step      一次工具调用的 journal：intent → result       崩溃恢复时判断“做没做过”，幂等对账
Event     细粒度事件流（带 seq）                        流式展示、断线续传、审计与回放
========  ==========================================  =================================

关键技巧
--------
- **只追加**：Entry 与 Event 从不修改；“分支”只是移动 session.head 指针（借鉴 pi 的会话树）。
- **CAS 状态迁移**：``UPDATE runs SET status=?, version=version+1 WHERE id=? AND status=? AND version=?``，
  影响行数为 0 即并发冲突（取消与完成同时到达时只有一个生效）。
- **WAL 模式**：进程被 ``kill -9`` 不会损坏数据库，已提交的事务全部保留。
- **事务**：工具结果写入 Entry 与 Step 完成标记在同一事务内，避免“结果写了但 journal 没写”。

生产环境可替换为 PostgreSQL（行锁 + 分区）+ 对象存储（大载荷）+ outbox（对外发布事件）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from .._utils import new_id, now, sha256_hex
from ..events import Event

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY,
  title TEXT,
  workspace TEXT,
  parent_session_id TEXT,
  head TEXT,
  meta TEXT NOT NULL DEFAULT '{}',
  created_at REAL,
  updated_at REAL
);
CREATE TABLE IF NOT EXISTS entries(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  id TEXT UNIQUE NOT NULL,
  session_id TEXT NOT NULL,
  parent_id TEXT,
  kind TEXT NOT NULL,
  run_id TEXT,
  payload TEXT NOT NULL,
  created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_entries_session ON entries(session_id, seq);
CREATE TABLE IF NOT EXISTS runs(
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  parent_run_id TEXT,
  status TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 0,
  attempt INTEGER NOT NULL DEFAULT 1,
  prompt TEXT,
  config TEXT NOT NULL DEFAULT '{}',
  usage TEXT NOT NULL DEFAULT '{}',
  result TEXT,
  error TEXT,
  status_reason TEXT,
  worker_id TEXT,
  heartbeat_at REAL,
  checkpoint TEXT NOT NULL DEFAULT '{}',
  created_at REAL,
  updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_runs_session ON runs(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
CREATE TABLE IF NOT EXISTS steps(
  run_id TEXT NOT NULL,
  call_id TEXT NOT NULL,
  tool TEXT,
  effect TEXT,
  idem_key TEXT,
  input_hash TEXT,
  status TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 1,
  output TEXT,
  snapshot TEXT,
  started_at REAL,
  ended_at REAL,
  PRIMARY KEY(run_id, call_id)
);
CREATE TABLE IF NOT EXISTS events(
  run_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  type TEXT NOT NULL,
  data TEXT NOT NULL,
  ts REAL,
  PRIMARY KEY(run_id, seq)
);
CREATE TABLE IF NOT EXISTS approvals(
  id TEXT PRIMARY KEY,
  run_id TEXT,
  session_id TEXT,
  call_id TEXT,
  tool TEXT,
  args TEXT,
  summary TEXT,
  risk TEXT,
  reason TEXT,
  status TEXT NOT NULL,
  decided_by TEXT,
  decision_reason TEXT,
  edited_args TEXT,
  remember INTEGER DEFAULT 0,
  created_at REAL,
  expires_at REAL,
  decided_at REAL
);
CREATE TABLE IF NOT EXISTS blobs(
  scope TEXT NOT NULL,
  ref TEXT NOT NULL,
  size INTEGER,
  data TEXT,
  created_at REAL,
  expires_at REAL,
  PRIMARY KEY(scope, ref)
);
"""


# ====================================================================== 记录类型
@dataclass
class SessionRecord:
    id: str
    title: str
    workspace: str
    parent_session_id: str | None
    head: str | None
    meta: dict[str, Any]
    created_at: float
    updated_at: float

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class Entry:
    id: str
    session_id: str
    parent_id: str | None
    kind: str          # message | compaction | label
    payload: dict[str, Any]
    run_id: str | None = None
    seq: int = 0
    created_at: float = field(default_factory=now)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class RunRecord:
    id: str
    session_id: str
    parent_run_id: str | None
    status: str
    version: int
    attempt: int
    prompt: str | None
    config: dict[str, Any]
    usage: dict[str, Any]
    result: dict[str, Any] | None
    error: str | None
    status_reason: str | None
    worker_id: str | None
    heartbeat_at: float | None
    checkpoint: dict[str, Any]
    created_at: float
    updated_at: float

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class StepRecord:
    run_id: str
    call_id: str
    tool: str
    effect: str
    idem_key: str
    input_hash: str
    status: str        # started | completed | failed | unknown | cancelled
    attempt: int
    output: dict[str, Any] | None
    snapshot: str | None
    started_at: float | None
    ended_at: float | None


@dataclass
class ApprovalRecord:
    id: str
    run_id: str
    session_id: str
    call_id: str
    tool: str
    args: dict[str, Any]
    summary: str
    risk: str
    reason: str
    status: str        # pending | approved | rejected | expired
    decided_by: str | None = None
    decision_reason: str | None = None
    edited_args: dict[str, Any] | None = None
    remember: bool = False
    created_at: float = field(default_factory=now)
    expires_at: float | None = None
    decided_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


_JSON_COLS = {"meta", "payload", "config", "usage", "result", "checkpoint", "output", "args", "edited_args"}


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    for k in _JSON_COLS & d.keys():
        if isinstance(d[k], str):
            d[k] = json.loads(d[k])
    return d


def _dump(v: Any) -> str | None:
    return None if v is None else json.dumps(v, ensure_ascii=False, default=str)


# ====================================================================== Store
class Store:
    """线程安全的 SQLite 存储。单连接 + 可重入锁；``tx()`` 提供可嵌套事务。"""

    def __init__(self, path: str | Path = ":memory:"):
        self.db_path = str(path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._depth = 0
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")  # WAL 下可抵御进程崩溃；抵御断电需 FULL
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
        self.blobs = BlobStore(self)

    # ------------------------------------------------------------------ 事务
    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            outer = self._depth == 0
            if outer:
                self._conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield self._conn
            except BaseException:
                self._depth -= 1
                if outer:
                    self._conn.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if outer:
                    self._conn.execute("COMMIT")

    def _q(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _x(self, sql: str, params: tuple | list = ()) -> int:
        with self._lock:
            return self._conn.execute(sql, params).rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ================================================================== sessions
    def create_session(
        self, title: str = "新会话", workspace: str = ".", parent_session_id: str | None = None,
        meta: dict | None = None, session_id: str | None = None,
    ) -> SessionRecord:
        sid = session_id or new_id("s_")
        t = now()
        self._x(
            "INSERT INTO sessions(id,title,workspace,parent_session_id,head,meta,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (sid, title, workspace, parent_session_id, None, _dump(meta or {}), t, t),
        )
        return self.get_session(sid)  # type: ignore[return-value]

    def get_session(self, sid: str) -> SessionRecord | None:
        rows = self._q("SELECT * FROM sessions WHERE id=?", (sid,))
        d = _row(rows[0]) if rows else None
        return SessionRecord(**d) if d else None

    def list_sessions(self, limit: int = 100, include_children: bool = False) -> list[SessionRecord]:
        where = "" if include_children else "WHERE parent_session_id IS NULL"
        rows = self._q(f"SELECT * FROM sessions {where} ORDER BY updated_at DESC LIMIT ?", (limit,))
        return [SessionRecord(**_row(r)) for r in rows]  # type: ignore[arg-type]

    def update_session(self, sid: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = now()
        cols = ", ".join(f"{k}=?" for k in fields)
        vals = [_dump(v) if k == "meta" else v for k, v in fields.items()]
        self._x(f"UPDATE sessions SET {cols} WHERE id=?", (*vals, sid))

    # ================================================================== entries
    _HEAD = object()

    def append_entry(
        self, session_id: str, kind: str, payload: dict[str, Any], *, run_id: str | None = None,
        parent_id: Any = _HEAD, entry_id: str | None = None,
    ) -> Entry:
        """追加一个条目并把 session.head 移到它上面。默认挂在当前 head 之下。"""
        with self.tx() as c:
            if parent_id is Store._HEAD:
                r = c.execute("SELECT head FROM sessions WHERE id=?", (session_id,)).fetchone()
                if r is None:
                    raise KeyError(f"会话不存在：{session_id}")
                parent_id = r["head"]
            eid = entry_id or new_id("e_")
            t = now()
            cur = c.execute(
                "INSERT INTO entries(id,session_id,parent_id,kind,run_id,payload,created_at) VALUES(?,?,?,?,?,?,?)",
                (eid, session_id, parent_id, kind, run_id, _dump(payload), t),
            )
            c.execute("UPDATE sessions SET head=?, updated_at=? WHERE id=?", (eid, t, session_id))
            return Entry(eid, session_id, parent_id, kind, payload, run_id, cur.lastrowid or 0, t)

    def get_entry(self, eid: str) -> Entry | None:
        rows = self._q("SELECT * FROM entries WHERE id=?", (eid,))
        return Entry(**_row(rows[0])) if rows else None  # type: ignore[arg-type]

    def path(self, session_id: str, head: str | None = None) -> list[Entry]:
        """从根走到 head 的路径（递归 CTE）。这就是“当前分支”的完整历史。"""
        if head is None:
            s = self.get_session(session_id)
            head = s.head if s else None
        if head is None:
            return []
        rows = self._q(
            """
            WITH RECURSIVE p(id, parent_id, depth) AS (
              SELECT id, parent_id, 0 FROM entries WHERE id = ?
              UNION ALL
              SELECT e.id, e.parent_id, p.depth + 1 FROM entries e JOIN p ON e.id = p.parent_id
            )
            SELECT entries.* FROM entries JOIN p ON entries.id = p.id ORDER BY p.depth DESC
            """,
            (head,),
        )
        return [Entry(**_row(r)) for r in rows]  # type: ignore[arg-type]

    def list_entries(self, session_id: str) -> list[Entry]:
        rows = self._q("SELECT * FROM entries WHERE session_id=? ORDER BY seq", (session_id,))
        return [Entry(**_row(r)) for r in rows]  # type: ignore[arg-type]

    # ================================================================== runs
    def create_run(
        self, session_id: str, prompt: str | None, *, parent_run_id: str | None = None,
        config: dict | None = None, status: str = "created", run_id: str | None = None,
    ) -> RunRecord:
        rid = run_id or new_id("r_")
        t = now()
        self._x(
            "INSERT INTO runs(id,session_id,parent_run_id,status,version,attempt,prompt,config,usage,checkpoint,created_at,updated_at)"
            " VALUES(?,?,?,?,0,1,?,?,'{}','{}',?,?)",
            (rid, session_id, parent_run_id, status, prompt, _dump(config or {}), t, t),
        )
        return self.get_run(rid)  # type: ignore[return-value]

    def get_run(self, rid: str) -> RunRecord | None:
        rows = self._q("SELECT * FROM runs WHERE id=?", (rid,))
        return RunRecord(**_row(rows[0])) if rows else None  # type: ignore[arg-type]

    def list_runs(self, session_id: str | None = None, limit: int = 100) -> list[RunRecord]:
        if session_id:
            rows = self._q("SELECT * FROM runs WHERE session_id=? ORDER BY created_at DESC LIMIT ?", (session_id, limit))
        else:
            rows = self._q("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,))
        return [RunRecord(**_row(r)) for r in rows]  # type: ignore[arg-type]

    def child_runs(self, parent_run_id: str) -> list[RunRecord]:
        rows = self._q("SELECT * FROM runs WHERE parent_run_id=? ORDER BY created_at", (parent_run_id,))
        return [RunRecord(**_row(r)) for r in rows]  # type: ignore[arg-type]

    def runs_with_status(self, *statuses: str) -> list[RunRecord]:
        q = ",".join("?" * len(statuses))
        rows = self._q(f"SELECT * FROM runs WHERE status IN ({q}) ORDER BY created_at", statuses)
        return [RunRecord(**_row(r)) for r in rows]  # type: ignore[arg-type]

    def cas_run(self, rid: str, from_status: str, from_version: int, to_status: str, **fields: Any) -> bool:
        """乐观锁状态迁移：只有 (status, version) 都与预期一致才会成功。"""
        sets = ["status=?", "version=version+1", "updated_at=?"]
        vals: list[Any] = [to_status, now()]
        for k, v in fields.items():
            sets.append(f"{k}=?")
            vals.append(_dump(v) if k in _JSON_COLS else v)
        n = self._x(
            f"UPDATE runs SET {', '.join(sets)} WHERE id=? AND status=? AND version=?",
            (*vals, rid, from_status, from_version),
        )
        return n == 1

    def update_run(self, rid: str, **fields: Any) -> None:
        """更新非状态字段（用量、检查点、结果）。状态字段必须走 cas_run。"""
        assert "status" not in fields, "状态迁移请使用 cas_run / RunStateMachine"
        fields["updated_at"] = now()
        cols = ", ".join(f"{k}=?" for k in fields)
        vals = [_dump(v) if k in _JSON_COLS else v for k, v in fields.items()]
        self._x(f"UPDATE runs SET {cols} WHERE id=?", (*vals, rid))

    def heartbeat(self, rid: str, worker_id: str) -> bool:
        """续租：只有租约仍属于自己时才成功（条件更新，避免“先检查再写入”的竞态把新主人的租约覆盖掉）。"""
        return self._x("UPDATE runs SET heartbeat_at=? WHERE id=? AND (worker_id IS NULL OR worker_id=?)",
                       (now(), rid, worker_id)) == 1

    def stale_runs(self, statuses: tuple[str, ...], older_than: float) -> list[RunRecord]:
        q = ",".join("?" * len(statuses))
        rows = self._q(
            f"SELECT * FROM runs WHERE status IN ({q}) AND (heartbeat_at IS NULL OR heartbeat_at < ?)",
            (*statuses, now() - older_than),
        )
        return [RunRecord(**_row(r)) for r in rows]  # type: ignore[arg-type]

    # ================================================================== steps（工具调用 journal）
    def step_started(
        self, run_id: str, call_id: str, *, tool: str, effect: str, idem_key: str,
        input_hash: str, snapshot: str | None = None,
    ) -> None:
        """预写 intent：在真正执行副作用 **之前** 落盘。重复写入（恢复后重试）时 attempt + 1。"""
        self._x(
            """INSERT INTO steps(run_id,call_id,tool,effect,idem_key,input_hash,status,attempt,snapshot,started_at)
               VALUES(?,?,?,?,?,?,'started',1,?,?)
               ON CONFLICT(run_id, call_id) DO UPDATE SET status='started', attempt=attempt+1,
                 started_at=excluded.started_at, input_hash=excluded.input_hash""",
            (run_id, call_id, tool, effect, idem_key, input_hash, snapshot, now()),
        )

    def step_finished(self, run_id: str, call_id: str, status: str, output: dict | None = None) -> None:
        self._x(
            "UPDATE steps SET status=?, output=?, ended_at=? WHERE run_id=? AND call_id=?",
            (status, _dump(output), now(), run_id, call_id),
        )

    def get_step(self, run_id: str, call_id: str) -> StepRecord | None:
        rows = self._q("SELECT * FROM steps WHERE run_id=? AND call_id=?", (run_id, call_id))
        return StepRecord(**_row(rows[0])) if rows else None  # type: ignore[arg-type]

    def list_steps(self, run_id: str) -> list[StepRecord]:
        rows = self._q("SELECT * FROM steps WHERE run_id=? ORDER BY started_at", (run_id,))
        return [StepRecord(**_row(r)) for r in rows]  # type: ignore[arg-type]

    # ================================================================== events
    def append_events(self, run_id: str, events: list[Event]) -> None:
        if not events:
            return
        with self.tx() as c:  # 一批一个事务：autocommit 下逐行提交要慢一个数量级
            c.executemany(
                "INSERT OR IGNORE INTO events(run_id,seq,type,data,ts) VALUES(?,?,?,?,?)",
                [(run_id, e.seq, e.type, _dump(e.data), e.ts) for e in events],
            )

    def append_event_next(self, run_id: str, type: str, data: dict[str, Any]) -> int:
        """没有活动 EventHub 时追加一条事件：序号在同一事务内分配（max + 1），不会和并发写入撞号。"""
        with self.tx() as c:
            seq = int(c.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE run_id=?", (run_id,)).fetchone()[0])
            c.execute("INSERT INTO events(run_id,seq,type,data,ts) VALUES(?,?,?,?,?)", (run_id, seq, type, _dump(data), now()))
        return seq

    def list_events(self, run_id: str, after_seq: int = 0, limit: int | None = None) -> list[Event]:
        sql = "SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq"
        params: list[Any] = [run_id, after_seq]
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return [
            Event(r["type"], json.loads(r["data"]), run_id, r["seq"], r["ts"]) for r in self._q(sql, params)
        ]

    def max_event_seq(self, run_id: str) -> int:
        r = self._q("SELECT MAX(seq) AS m FROM events WHERE run_id=?", (run_id,))
        return int(r[0]["m"] or 0)

    # ================================================================== approvals
    def create_approval(self, a: ApprovalRecord) -> ApprovalRecord:
        self._x(
            "INSERT INTO approvals(id,run_id,session_id,call_id,tool,args,summary,risk,reason,status,created_at,expires_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (a.id, a.run_id, a.session_id, a.call_id, a.tool, _dump(a.args), a.summary, a.risk, a.reason,
             a.status, a.created_at, a.expires_at),
        )
        return a

    def get_approval(self, aid: str) -> ApprovalRecord | None:
        rows = self._q("SELECT * FROM approvals WHERE id=?", (aid,))
        if not rows:
            return None
        d = _row(rows[0])
        d["remember"] = bool(d.get("remember"))  # type: ignore[index]
        return ApprovalRecord(**d)  # type: ignore[arg-type]

    def resolve_approval(
        self, aid: str, status: str, *, decided_by: str, reason: str = "",
        edited_args: dict | None = None, remember: bool = False,
    ) -> bool:
        """CAS：只有 pending 状态的审批能被决定一次（审批与超时/取消并发时只有一个生效）。"""
        n = self._x(
            "UPDATE approvals SET status=?, decided_by=?, decision_reason=?, edited_args=?, remember=?, decided_at=?"
            " WHERE id=? AND status='pending'",
            (status, decided_by, reason, _dump(edited_args), int(remember), now(), aid),
        )
        return n == 1

    def list_approvals(self, status: str | None = "pending", run_id: str | None = None) -> list[ApprovalRecord]:
        sql, params = "SELECT id FROM approvals WHERE 1=1", []
        if status:
            sql += " AND status=?"
            params.append(status)
        if run_id:
            sql += " AND run_id=?"
            params.append(run_id)
        return [a for r in self._q(sql + " ORDER BY created_at", params) if (a := self.get_approval(r["id"]))]


class BlobStore:
    """内容寻址的大载荷存储：超大工具结果外置到这里，上下文只放句柄。

    - 内容寻址：ref = sha256 前缀，相同内容只存一份；
    - 按 scope（会话）隔离：句柄跨会话不可读，防止数据泄漏；
    - 支持 TTL：过期清理。
    生产环境可替换为对象存储（S3/OSS）+ 签名 URL。
    """

    def __init__(self, store: Store):
        self._s = store

    def put(self, scope: str, text: str, ttl: float | None = None) -> str:
        ref = "blob_" + sha256_hex(text)[:16]
        t = now()
        self._s._x(
            "INSERT INTO blobs(scope,ref,size,data,created_at,expires_at) VALUES(?,?,?,?,?,?)"
            " ON CONFLICT(scope, ref) DO UPDATE SET expires_at=excluded.expires_at",
            (scope, ref, len(text), text, t, t + ttl if ttl else None),
        )
        return ref

    def get(self, scope: str, ref: str) -> str | None:
        rows = self._s._q("SELECT data, expires_at FROM blobs WHERE scope=? AND ref=?", (scope, ref))
        if not rows or (rows[0]["expires_at"] and rows[0]["expires_at"] < now()):
            return None
        return rows[0]["data"]

    def purge_expired(self) -> int:
        return self._s._x("DELETE FROM blobs WHERE expires_at IS NOT NULL AND expires_at < ?", (now(),))
