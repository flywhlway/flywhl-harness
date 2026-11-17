"""内置工具的失败路径与边界行为：文件工具拒绝修改的情形与截断提示，bash 的流式解码、墙钟预算与进程组收尾，
出站代理对坏请求和解析失败的答复，沙箱配置的启动校验，以及 Seatbelt、bubblewrap 启动参数在各种宿主环境下的取舍。

载荷都是临时目录里的哨兵文件，进程组用例只结束用例自己启动的 ``sleep``。bubblewrap 用例只检查生成的 argv，不启动 bwrap。
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import ipaddress
import itertools
import os
import shutil
import signal
import socket
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from flywhl_harness.cancel import CancelToken
from flywhl_harness.runtime.workspace import Workspace
from flywhl_harness.tools import sandbox
from flywhl_harness.tools.base import ToolContext
from flywhl_harness.tools.confine import Confinement, _remove_tmp
from flywhl_harness.tools.egress import EgressProxy, parse_rules
from flywhl_harness.tools.fs import (
    EditTool,
    GrepTool,
    LsTool,
    ReadTool,
    WriteTool,
    _human,
)
from flywhl_harness.tools.shell import BashTool, _clean_env, _reap_group, _terminate


def _ctx(root: Path, **kw) -> ToolContext:
    return ToolContext(run_id="r", session_id="s", call_id="c", workspace=Workspace(root), cancel=CancelToken(), blobs=None,
                       idempotency_key="k", **kw)


def _touch_later(p: Path) -> None:
    """模拟文件被外部改动：只改时间戳就够了，过期判定看的是 mtime。"""
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


# ---------------------------------------------------------------------- read
async def test_read_explains_missing_directory_and_binary(ws):
    (ws / "sub").mkdir()
    (ws / "blob").write_bytes(b"ab\x00cd")
    ctx = _ctx(ws)
    missing = await ReadTool().execute({"path": "nope.txt"}, ctx)
    assert missing.is_error and "文件不存在：nope.txt" in missing.content
    folder = await ReadTool().execute({"path": "sub"}, ctx)
    assert folder.is_error and "是目录" in folder.content
    binary = await ReadTool().execute({"path": "blob"}, ctx)
    assert binary.is_error and "二进制文件（5 字节）" in binary.content
    assert "file_mtimes" not in ctx.state   # 读失败不算读过，之后的 edit 不能据此通过过期检查


async def test_read_offset_past_the_end_reports_total(ws):
    (ws / "a.txt").write_text("一\n二\n三\n", encoding="utf-8")
    res = await ReadTool().execute({"path": "a.txt", "offset": 4}, _ctx(ws))
    assert res.is_error and "offset=4 超出文件总行数 3" in res.content


async def test_read_empty_file_is_not_an_error(ws):
    (ws / "empty.txt").touch()
    res = await ReadTool().execute({"path": "empty.txt"}, _ctx(ws))
    assert not res.is_error and res.content.startswith("(空文件)")
    assert res.details["total_lines"] == 0


async def test_read_long_file_tells_how_to_continue(ws):
    (ws / "long.txt").write_text("".join(f"第{i}行\n" for i in range(1, 2501)), encoding="utf-8")
    first = await ReadTool().execute({"path": "long.txt"}, _ctx(ws))
    assert "[已显示第 1–2000 行，共 2500 行。继续读取请使用 offset=2001]" in first.content
    assert first.details["end"] == 2000
    rest = await ReadTool().execute({"path": "long.txt", "offset": 2001}, _ctx(ws))
    assert "第2500行" in rest.content and "继续读取" not in rest.content


async def test_read_stops_at_the_byte_limit_on_a_whole_line(ws):
    """2000 行以内，但总字节超过 50KB：按整行截断，footer 的 offset 接得上。"""
    (ws / "wide.txt").write_text("".join(f"{i:04d}" + "x" * 96 + "\n" for i in range(1000)), encoding="utf-8")
    res = await ReadTool().execute({"path": "wide.txt"}, _ctx(ws))
    end = res.details["end"]
    assert 400 < end < 1000 and f"继续读取请使用 offset={end + 1}" in res.content
    assert len(res.content.encode("utf-8")) < 51 * 1024
    assert f"{end - 1:04d}" in res.content and f"{end:04d}" + "x" not in res.content


async def test_read_cuts_a_very_long_line_and_marks_it(ws):
    (ws / "line.txt").write_text("a" * 2500 + "\n", encoding="utf-8")
    res = await ReadTool().execute({"path": "line.txt"}, _ctx(ws))
    assert "a" * 2000 + "…" in res.content and "a" * 2001 not in res.content


# ---------------------------------------------------------------------- write
async def test_write_refuses_after_external_change(ws):
    f = ws / "a.txt"
    f.write_text("旧\n", encoding="utf-8")
    ctx = _ctx(ws)
    await ReadTool().execute({"path": "a.txt"}, ctx)
    _touch_later(f)
    res = await WriteTool().execute({"path": "a.txt", "content": "新\n"}, ctx)
    assert res.is_error and "已被外部修改" in res.content
    assert f.read_text(encoding="utf-8") == "旧\n"


async def test_write_will_not_overwrite_binary_or_non_utf8(ws):
    """整体覆盖前先读一遍是为了出 diff；读不了的文件不覆盖，否则模型看不到自己毁掉了什么。"""
    blob, gbk = ws / "blob", ws / "gbk.txt"
    blob.write_bytes(b"\x00\x01\x02")
    gbk.write_bytes("中文".encode("gbk"))
    ctx = _ctx(ws)
    res = await WriteTool().execute({"path": "blob", "content": "x"}, ctx)
    assert res.is_error and "二进制" in res.content and blob.read_bytes() == b"\x00\x01\x02"
    res = await WriteTool().execute({"path": "gbk.txt", "content": "x"}, ctx)
    assert res.is_error and "UTF-8" in res.content and gbk.read_bytes() == "中文".encode("gbk")


async def test_write_counts_lines_with_and_without_final_newline(ws):
    ctx = _ctx(ws)
    no_nl = await WriteTool().execute({"path": "a.txt", "content": "一\n二"}, ctx)
    assert no_nl.details["created"] and "（7 字节，2 行）" in no_nl.content
    again = await WriteTool().execute({"path": "a.txt", "content": "一\n二\n"}, ctx)
    assert not again.details["created"] and again.content.startswith("已覆盖") and "2 行" in again.content
    empty = await WriteTool().execute({"path": "b/c.txt", "content": ""}, ctx)
    assert "0 字节，0 行" in empty.content and (ws / "b" / "c.txt").read_text(encoding="utf-8") == ""


async def test_write_diff_for_the_ui_is_capped(ws):
    f = ws / "big.txt"
    f.write_text("".join(f"旧{i}\n" for i in range(300)), encoding="utf-8")
    res = await WriteTool().execute({"path": "big.txt", "content": "".join(f"新{i}\n" for i in range(300))}, _ctx(ws))
    lines = res.details["diff"].splitlines()
    assert len(lines) == 401 and lines[-1].startswith("... 差异过长，已省略 ")
    assert f.read_text(encoding="utf-8").count("\n") == 300   # 省略的只是展示，文件是完整的


def test_write_preview_gives_up_quietly_on_what_it_cannot_show(ws):
    (ws / "sub").mkdir()
    (ws / "blob").write_bytes(b"\x00")
    ctx, tool = _ctx(ws), WriteTool()
    assert tool.preview({"path": "sub", "content": "x"}, ctx) is None
    assert tool.preview({"path": str(Path.home() / ".ssh" / "id_rsa"), "content": "x"}, ctx) is None
    # 读不出的旧内容按空文件算，审批时看到的是整份新内容
    assert "+x" in tool.preview({"path": "blob", "content": "x"}, ctx)


# ---------------------------------------------------------------------- edit
async def test_edit_missing_file_points_to_write(ws):
    res = await EditTool().execute({"path": "nope.txt", "old_string": "a", "new_string": "b"}, _ctx(ws))
    assert res.is_error and "文件不存在" in res.content and "write" in res.content
    assert not (ws / "nope.txt").exists()


async def test_edit_refuses_after_external_change(ws):
    f = ws / "a.txt"
    f.write_text("甲\n", encoding="utf-8")
    ctx = _ctx(ws)
    await ReadTool().execute({"path": "a.txt"}, ctx)
    _touch_later(f)
    res = await EditTool().execute({"path": "a.txt", "old_string": "甲", "new_string": "乙"}, ctx)
    assert res.is_error and "a.txt 在你上次读取后已被外部修改" in res.content
    assert f.read_text(encoding="utf-8") == "甲\n"
    await ReadTool().execute({"path": "a.txt"}, ctx)   # 重新读过就放行
    res = await EditTool().execute({"path": "a.txt", "old_string": "甲", "new_string": "乙"}, ctx)
    assert not res.is_error and f.read_text(encoding="utf-8") == "乙\n"


async def test_stale_check_tolerates_a_file_that_was_deleted(ws):
    """读过之后文件被删：不算过期，走“文件不存在”或者重新创建。"""
    f = ws / "a.txt"
    f.write_text("甲\n", encoding="utf-8")
    ctx = _ctx(ws)
    await ReadTool().execute({"path": "a.txt"}, ctx)
    f.unlink()
    res = await WriteTool().execute({"path": "a.txt", "content": "乙\n"}, ctx)
    assert not res.is_error and res.details["created"]


async def test_edit_rejects_noop_and_empty_old_string(ws):
    f = ws / "a.txt"
    f.write_text("甲\n", encoding="utf-8")
    ctx = _ctx(ws)
    same = await EditTool().execute({"path": "a.txt", "old_string": "甲", "new_string": "甲"}, ctx)
    assert same.is_error and "相同" in same.content
    empty = await EditTool().execute({"path": "a.txt", "old_string": "", "new_string": "乙"}, ctx)
    assert empty.is_error and "不能为空" in empty.content
    assert f.read_text(encoding="utf-8") == "甲\n"


async def test_edit_will_not_touch_binary_or_non_utf8(ws):
    blob, gbk = ws / "blob", ws / "gbk.txt"
    blob.write_bytes(b"ab\x00cd")
    gbk.write_bytes("中文".encode("gbk"))
    ctx = _ctx(ws)
    res = await EditTool().execute({"path": "blob", "old_string": "ab", "new_string": "zz"}, ctx)
    assert res.is_error and "二进制文件" in res.content and blob.read_bytes() == b"ab\x00cd"
    res = await EditTool().execute({"path": "gbk.txt", "old_string": "中", "new_string": "英"}, ctx)
    assert res.is_error and "不是 UTF-8" in res.content and gbk.read_bytes() == "中文".encode("gbk")


async def test_edit_not_found_hints_at_whitespace_mismatch(ws):
    """首行在文件里出现，说明大概率是缩进不对：告诉模型在哪几行。"""
    f = ws / "a.py"
    f.write_text("def f():\n    return 1\n\n\ndef g():\n    return 1\n", encoding="utf-8")
    res = await EditTool().execute({"path": "a.py", "old_string": "return 1\n  # 尾注", "new_string": "x"}, _ctx(ws))
    assert res.is_error and "未找到 old_string" in res.content
    assert "首行内容在第 [2, 6] 行附近出现" in res.content


async def test_edit_not_found_without_a_usable_hint(ws):
    f = ws / "a.txt"
    f.write_text("甲\n", encoding="utf-8")
    ctx = _ctx(ws)
    absent = await EditTool().execute({"path": "a.txt", "old_string": "乙", "new_string": "丙"}, ctx)
    assert absent.is_error and "未找到 old_string" in absent.content and "附近" not in absent.content
    blank = await EditTool().execute({"path": "a.txt", "old_string": "   ", "new_string": "丙"}, ctx)
    assert blank.is_error and "未找到 old_string" in blank.content and "附近" not in blank.content
    assert f.read_text(encoding="utf-8") == "甲\n"


async def test_edit_ambiguous_match_lists_lines_and_replace_all_resolves_it(ws):
    f = ws / "a.txt"
    f.write_text("x\ny\nx\nz\nx\n", encoding="utf-8")
    ctx = _ctx(ws)
    res = await EditTool().execute({"path": "a.txt", "old_string": "x", "new_string": "w"}, ctx)
    assert res.is_error and "出现 3 次（第 [1, 3, 5] 行）" in res.content
    assert f.read_text(encoding="utf-8") == "x\ny\nx\nz\nx\n"
    res = await EditTool().execute({"path": "a.txt", "old_string": "x", "new_string": "w", "replace_all": True}, ctx)
    assert not res.is_error and res.details["replacements"] == 3 and "替换 3 处" in res.content
    assert f.read_text(encoding="utf-8") == "w\ny\nw\nz\nw\n"


async def test_edit_ambiguous_match_lists_at_most_ten_lines(ws):
    f = ws / "a.txt"
    f.write_text("x\n" * 12, encoding="utf-8")
    res = await EditTool().execute({"path": "a.txt", "old_string": "x", "new_string": "w"}, _ctx(ws))
    assert "出现 12 次（第 [1, 2, 3, 4, 5, 6, 7, 8, 9, 10] 行）" in res.content


def test_edit_preview_only_answers_when_the_edit_would_apply(ws):
    (ws / "a.txt").write_text("甲甲\n", encoding="utf-8")
    (ws / "sub").mkdir()
    ctx, tool = _ctx(ws), EditTool()
    assert tool.preview({"path": "a.txt", "old_string": "乙", "new_string": "丙"}, ctx) is None
    assert tool.preview({"path": "a.txt", "old_string": "", "new_string": "丙"}, ctx) is None
    assert tool.preview({"path": "nope.txt", "old_string": "甲", "new_string": "丙"}, ctx) is None
    assert tool.preview({"path": "sub", "old_string": "甲", "new_string": "丙"}, ctx) is None
    assert "+丙甲" in tool.preview({"path": "a.txt", "old_string": "甲", "new_string": "丙"}, ctx)
    assert "+丙丙" in tool.preview({"path": "a.txt", "old_string": "甲", "new_string": "丙", "replace_all": True}, ctx)


# ---------------------------------------------------------------------- ls
async def test_ls_rejects_a_file_or_missing_path(ws):
    (ws / "a.txt").touch()
    for path in ("a.txt", "nope"):
        res = await LsTool().execute({"path": path}, _ctx(ws))
        assert res.is_error and f"{path} 不是目录或不存在" in res.content


async def test_ls_empty_directory(ws):
    res = await LsTool().execute({}, _ctx(ws))
    assert res.content == "(空目录)" and res.details["count"] == 0


async def test_ls_lists_directories_first_and_recurses_to_depth(ws):
    (ws / "b" / "c" / "d").mkdir(parents=True)
    (ws / "b" / "c" / "deep.txt").write_bytes(b"x" * 2048)
    (ws / "a.txt").write_bytes(b"xy")
    shallow = await LsTool().execute({}, _ctx(ws))
    assert shallow.content.splitlines() == ["b/", "a.txt  (2B)"]
    deep = await LsTool().execute({"depth": 3}, _ctx(ws))
    assert deep.content.splitlines() == ["b/", "  c/", "    d/", "    deep.txt  (2.0KB)", "a.txt  (2B)"]
    assert deep.details["count"] == 5


async def test_ls_skips_noise_but_only_directories_by_that_name(ws):
    (ws / "node_modules").mkdir()
    (ws / "node_modules" / "pkg.js").touch()
    (ws / ".git").mkdir()
    (ws / ".DS_Store").touch()
    (ws / "src").mkdir()
    (ws / "src" / "__pycache__").mkdir()
    (ws / "src" / "Thumbs.db").touch()
    (ws / "src" / "dist").touch()   # 同名的普通文件不是构建产物目录，要列出来
    res = await LsTool().execute({"depth": 3}, _ctx(ws))
    assert res.content.splitlines() == ["src/", "  dist  (0B)"]


async def test_ls_dangling_symlink_is_listed_with_zero_size(ws):
    (ws / "broken").symlink_to(ws / "gone")
    res = await LsTool().execute({}, _ctx(ws))
    assert res.content == "broken  (0B)"


@pytest.mark.skipif(os.geteuid() == 0, reason="root 不受目录权限限制")
async def test_ls_keeps_going_past_an_unreadable_directory(ws):
    locked = ws / "locked"
    locked.mkdir()
    (ws / "z.txt").touch()
    locked.chmod(0)
    try:
        res = await LsTool().execute({"depth": 2}, _ctx(ws))
    finally:
        locked.chmod(0o700)
    assert not res.is_error and res.content.splitlines() == ["locked/", "z.txt  (0B)"]


async def test_ls_truncation_is_reported_only_when_entries_remain(ws):
    for i in range(500):
        (ws / f"{i:03d}.txt").touch()
    exact = await LsTool().execute({}, _ctx(ws))
    assert exact.details["count"] == 500 and "条目过多" not in exact.content
    (ws / "500.txt").touch()
    over = await LsTool().execute({}, _ctx(ws))
    assert over.details["count"] == 500 and "[条目过多，仅显示前 500 项" in over.content
    assert "500.txt" not in over.content


def test_human_size_units():
    assert _human(1023) == "1023B"
    assert _human(1536) == "1.5KB"
    assert _human(5 * 1024**2) == "5.0MB"
    assert _human(3 * 1024**3) == "3.0GB"
    assert _human(2 * 1024**4) == "2.0TB"


# ---------------------------------------------------------------------- grep
async def test_grep_bad_regex_fails_before_searching(ws):
    res = await GrepTool().execute({"pattern": "("}, _ctx(ws))
    assert res.is_error and "正则表达式无效" in res.content and "转义" in res.content


# ---------------------------------------------------------------------- bash：环境变量
def test_clean_env_keeps_secrets_only_when_told_to(monkeypatch):
    monkeypatch.setenv("FLYWHL_GAPS_API_KEY", "哨兵")
    assert "FLYWHL_GAPS_API_KEY" not in _clean_env(True)
    assert _clean_env(False)["FLYWHL_GAPS_API_KEY"] == "哨兵"


@pytest.mark.parametrize(("count", "first"), [("2", 2), ("0", 0), ("-3", 0), ("不是数字", 0)])
def test_clean_env_appends_git_hardening_after_user_config(monkeypatch, count, first):
    """接在用户已有的 GIT_CONFIG_* 后面；计数写坏时从 0 开始，防护项不能丢。"""
    monkeypatch.setenv("GIT_CONFIG_COUNT", count)
    for i in range(first):
        monkeypatch.setenv(f"GIT_CONFIG_KEY_{i}", f"user.k{i}")
        monkeypatch.setenv(f"GIT_CONFIG_VALUE_{i}", "v")
    env = _clean_env(True)
    assert env[f"GIT_CONFIG_KEY_{first}"] == "core.fsmonitor" and env[f"GIT_CONFIG_VALUE_{first}"] == "false"
    assert env[f"GIT_CONFIG_KEY_{first + 1}"] == "safe.bareRepository"
    assert env["GIT_CONFIG_COUNT"] == str(first + 2)


async def test_bash_sees_secret_env_when_stripping_is_off(ws, monkeypatch):
    monkeypatch.setenv("FLYWHL_GAPS_TOKEN", "哨兵")
    stripped = await BashTool(sandbox="off").execute({"command": 'echo "[$FLYWHL_GAPS_TOKEN]"'}, _ctx(ws))
    assert "[]" in stripped.content
    kept = await BashTool(sandbox="off", strip_secret_env=False).execute({"command": 'echo "[$FLYWHL_GAPS_TOKEN]"'}, _ctx(ws))
    assert "[哨兵]" in kept.content


# ---------------------------------------------------------------------- bash：流式解码
async def test_bash_holds_back_a_split_multibyte_character(ws):
    """第一块只有“中”的首字节：解码不出字符就不推送，第二块到了才一起推。"""
    pushed: list[str] = []
    cmd = r"printf '\344'; sleep 0.3; printf '\270\255'"
    res = await BashTool(sandbox="off").execute({"command": cmd}, _ctx(ws, emit=lambda u: pushed.append(u["output"])))
    assert pushed == ["中"] and res.content.startswith("中")


async def test_bash_flushes_a_truncated_character_at_end_of_output(ws):
    pushed: list[str] = []
    res = await BashTool(sandbox="off").execute({"command": r"printf '\344'"},
                                                _ctx(ws, emit=lambda u: pushed.append(u["output"])))
    assert pushed == ["\ufffd"] and not res.is_error and res.content.startswith("\ufffd")


# ---------------------------------------------------------------------- bash：墙钟预算
async def test_bash_timeout_is_clamped_to_the_run_deadline(ws):
    started = time.monotonic()
    res = await BashTool(sandbox="off").execute({"command": "sleep 30", "timeout": 120}, _ctx(ws, time_left=1.0))
    assert time.monotonic() - started < 10
    assert res.is_error and res.details["timed_out"] and "命令超时（1s）" in res.content


async def test_bash_explicit_timeout_wins_when_shorter_than_the_deadline(ws):
    res = await BashTool(sandbox="off").execute({"command": "sleep 30", "timeout": 1}, _ctx(ws, time_left=500.0))
    assert res.details["timed_out"] and "命令超时（1s）" in res.content


async def test_bash_nearly_spent_deadline_still_gets_one_second(ws):
    """Run 只剩几毫秒时不至于把命令当场判超时：下限 1 秒。"""
    res = await BashTool(sandbox="off").execute({"command": "echo done"}, _ctx(ws, time_left=0.001))
    assert not res.is_error and "done" in res.content


# ---------------------------------------------------------------------- bash：进程组
@contextlib.asynccontextmanager
async def _own_group(script: str):
    """起一个属于自己的进程组。脚本用 stdout 报告就绪；用例失败时收掉这一组，不留 sleep。"""
    proc = await asyncio.create_subprocess_exec("/bin/bash", "-c", script, stdout=asyncio.subprocess.PIPE,
                                                stdin=asyncio.subprocess.DEVNULL, start_new_session=True)
    try:
        yield proc
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()


async def _group_gone(pgid: int, within: float = 5.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except (ProcessLookupError, PermissionError):   # macOS 上组里只剩僵尸时报 EPERM
            return True
        await asyncio.sleep(0.02)
    return False


async def test_reap_group_ends_what_the_command_left_behind():
    async with _own_group("sleep 30 >/dev/null 2>&1 & echo $!") as proc:
        left = int(await proc.stdout.readline())
        await proc.wait()
        assert os.getpgid(left) == proc.pid   # 命令已经退出，后台的 sleep 还在组里
        await _reap_group(proc.pid)
        assert await _group_gone(proc.pid)


async def test_reap_group_escalates_when_sigterm_is_ignored():
    async with _own_group('trap "" TERM; sleep 30 >/dev/null 2>&1 & echo $!') as proc:
        await proc.stdout.readline()
        await proc.wait()
        started = time.monotonic()
        await _reap_group(proc.pid, grace=0.3)
        assert time.monotonic() - started >= 0.3   # 等满宽限期才动手，说明 SIGTERM 确实没起作用
        assert await _group_gone(proc.pid)


async def test_terminate_escalates_to_sigkill():
    async with _own_group('trap "" TERM; echo ready; exec sleep 30') as proc:
        await proc.stdout.readline()
        await _terminate(proc, grace=0.2)
        assert proc.returncode == -signal.SIGKILL


async def test_terminate_tolerates_a_process_that_is_already_gone():
    """asyncio 还没收到退出通知、进程已经没了：查不到进程组就算结束。pid 取一个不可能存在的值，不碰别的进程。"""
    await _terminate(SimpleNamespace(returncode=None, pid=2**30))   # 再往下走就会调用不存在的 wait()


@pytest.mark.os_sandbox
@pytest.mark.skipif(sandbox.probe().level == "", reason="需要可用的系统沙箱")
async def test_bash_ends_background_processes_when_the_proxy_closes(ws):
    """代理的端口随命令关闭、之后可能被别的服务复用：留在后台的进程不能活过这条命令。"""
    level = "macos" if sys.platform == "darwin" else "linux"
    marker = ws / "late"
    tool = BashTool(sandbox=level, sandbox_network="allowlist")
    res = await tool.execute({"command": f"(sleep 1; touch {marker}) >/dev/null 2>&1 &"}, _ctx(ws))
    assert not res.is_error
    await asyncio.sleep(1.5)
    assert not marker.exists()


async def test_bash_reports_timeout_when_a_background_process_holds_stdout(ws):
    """命令本身立刻退出，后台进程占着 stdout：读不到 EOF，按超时返回，退出码和已有输出都在。"""
    res = await BashTool(sandbox="off").execute({"command": "sleep 30 & echo $!", "timeout": 1}, _ctx(ws))
    left = int(res.content.splitlines()[0])
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(left, signal.SIGKILL)   # 只收掉这条命令留下的 sleep
    assert res.is_error and res.details["timed_out"] and res.details["exit_code"] == 0


# ---------------------------------------------------------------------- 出站代理
async def _ask(proxy: EgressProxy, request: bytes) -> str:
    r, w = await asyncio.open_connection("127.0.0.1", proxy.port)
    w.write(request)
    await w.drain()
    try:
        return (await asyncio.wait_for(r.read(), 5)).decode("utf-8")
    finally:
        w.close()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _answers(*ips: str):
    async def resolve(_host: str, _port: int):
        return [ipaddress.ip_address(ip) for ip in ips]
    return resolve


async def test_proxy_refuses_https_in_a_plain_request_line():
    async with EgressProxy(parse_rules(["pypi.org"])) as proxy:
        reply = await _ask(proxy, b"GET https://pypi.org/ HTTP/1.1\r\nHost: pypi.org\r\n\r\n")
    assert reply.startswith("HTTP/1.1 400 ") and "HTTPS 请用 CONNECT" in reply
    assert proxy.denied == []   # 写法问题，不算撞白名单


async def test_proxy_refuses_an_allowed_name_that_resolves_to_a_private_address():
    """DNS 重绑定：名字在白名单里，地址是内网。子域名通配不放行内网。"""
    async with EgressProxy(parse_rules(["*.example.com"]), resolve=_answers("10.0.0.5")) as proxy:
        reply = await _ask(proxy, b"CONNECT a.example.com:443 HTTP/1.1\r\n\r\n")
    assert reply.startswith("HTTP/1.1 403 ") and "解析到 10.0.0.5（内网地址）" in reply
    assert proxy.denied == ["a.example.com:443"]


async def test_proxy_reports_a_resolver_failure_as_bad_gateway():
    async def broken(_host: str, _port: int):
        raise socket.gaierror("没有这个主机")

    async with EgressProxy(parse_rules(["pypi.org"]), resolve=broken) as proxy:
        reply = await _ask(proxy, b"CONNECT pypi.org:443 HTTP/1.1\r\n\r\n")
    assert reply.startswith("HTTP/1.1 502 ") and "解析 pypi.org 失败：没有这个主机" in reply
    assert proxy.denied == []   # 连不上不是被白名单拦下，不能提示用户加白名单


async def test_proxy_gives_up_on_a_resolver_that_hangs():
    async def hang(_host: str, _port: int):
        await asyncio.sleep(30)

    async with EgressProxy(parse_rules(["pypi.org"]), resolve=hang, connect_timeout=0.05) as proxy:
        reply = await _ask(proxy, b"CONNECT pypi.org:443 HTTP/1.1\r\n\r\n")
    assert reply.startswith("HTTP/1.1 502 ") and "解析 pypi.org 失败" in reply


async def test_proxy_reports_when_no_address_accepts_the_connection():
    port = _free_port()
    async with EgressProxy(parse_rules([f"localhost:{port}"]), resolve=_answers("127.0.0.1")) as proxy:
        reply = await _ask(proxy, f"CONNECT localhost:{port} HTTP/1.1\r\n\r\n".encode())
    assert reply.startswith("HTTP/1.1 502 ") and f"连接 localhost:{port} 失败" in reply
    assert proxy.denied == []


async def test_proxy_survives_a_client_that_hangs_up_mid_request():
    async with EgressProxy([]) as proxy:
        r, w = await asyncio.open_connection("127.0.0.1", proxy.port)
        w.write(b"CONNECT example.com:443 HTTP/1.1\r\n")   # 请求头没写完就半关闭
        w.write_eof()
        assert await asyncio.wait_for(r.read(), 5) == b""
        w.close()
        reply = await _ask(proxy, b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")   # 下一个连接不受影响
    assert reply.startswith("HTTP/1.1 403 ") and proxy.denied == ["example.com:443"]


# ---------------------------------------------------------------------- 沙箱配置解析
@pytest.mark.parametrize(("args", "message"), [
    (("off", "wifi"), "未知的沙箱网络策略 'wifi'"),
    (("off", "auto", "pypi.org"), "network_allow 必须是字符串列表"),
    (("off", "auto", None, "/run/a.sock"), "unix_sockets 必须是绝对路径的列表"),
    (("off", "auto", None, ["relative.sock"]), "unix_sockets 必须是绝对路径的列表"),
    (("off", "auto", None, [123]), "unix_sockets 必须是绝对路径的列表"),
])
def test_confinement_rejects_bad_config_at_startup(args, message):
    with pytest.raises(ValueError, match=message):
        Confinement.resolve(*args)


def test_remove_tmp_only_deletes_its_own_directories(tmp_path, monkeypatch):
    """atexit 里执行的删除：目标不是自己建的就不动。"""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    mine = tmp_path / "flywhl-sandbox-tmp-a"
    foreign = tmp_path / "project"
    nested = tmp_path / "sub" / "flywhl-sandbox-tmp-b"
    for d in (mine, foreign, nested):
        d.mkdir(parents=True)
    _remove_tmp(str(foreign))
    _remove_tmp(str(nested))
    assert foreign.is_dir() and nested.is_dir()
    _remove_tmp(str(mine))
    assert not mine.exists()


# ---------------------------------------------------------------------- 系统沙箱：探测
def test_trial_turns_every_failure_into_text():
    assert sandbox._trial(["/bin/sh", "-c", ":"]) == ""
    assert sandbox._trial(["/bin/sh", "-c", "exit 3"]) == "退出码 3"
    assert sandbox._trial(["/bin/sh", "-c", "echo broken >&2; exit 1"]) == "broken"
    assert "flywhl-no-such-program" in sandbox._trial(["/nonexistent/flywhl-no-such-program"])   # 起不来也不抛异常


def test_probe_reports_a_missing_sandbox_exec(monkeypatch):
    monkeypatch.setattr(sandbox.sys, "platform", "darwin")
    monkeypatch.setattr(sandbox, "SANDBOX_EXEC", "/nonexistent/sandbox-exec")
    assert sandbox.probe() == sandbox.Detected("", "没有 /nonexistent/sandbox-exec")


def test_detect_probes_only_once(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(sandbox, "probe", lambda: calls.append(1) or sandbox.Detected("macos"))
    sandbox.detect.cache_clear()
    try:
        assert sandbox.detect() == sandbox.detect() == sandbox.Detected("macos")
    finally:
        sandbox.detect.cache_clear()   # 假结果不留给后面的用例
    assert len(calls) == 1


# ---------------------------------------------------------------------- 系统沙箱：Seatbelt 配置
def test_default_writable_is_the_system_temp_dirs():
    assert set(sandbox.default_writable()) == {os.path.realpath(tempfile.gettempdir()), "/private/tmp", "/private/var/folders"}


def test_seatbelt_profile_rejects_unknown_network(ws):
    with pytest.raises(ValueError, match="未知的沙箱网络策略 'auto'"):   # auto 要先解析成具体策略，到不了这里
        sandbox.seatbelt_profile(ws, network="auto")


def test_seatbelt_profile_denies_directories_named_by_credential_env(ws, tmp_path):
    gnupg = tmp_path / "gnupg"
    gnupg.mkdir()
    rule = f'(subpath "{os.path.realpath(gnupg)}")'
    assert rule in sandbox.seatbelt_profile(ws, writable=[], env={"GNUPGHOME": str(gnupg)}).splitlines()[2]
    assert rule not in sandbox.seatbelt_profile(ws, writable=[], env={}).splitlines()[2]


def test_seatbelt_profile_freezes_the_git_directory_itself(ws):
    rule = f"(literal \"{os.path.join(os.path.realpath(ws), '.git')}\")"
    assert rule not in sandbox.seatbelt_profile(ws, writable=[], env={})
    (ws / ".git").mkdir()
    assert rule in sandbox.seatbelt_profile(ws, writable=[], env={})


@pytest.mark.parametrize(("command", "program"), [("echo hi", ["/bin/bash", "-c", "echo hi"]),
                                                  (["/bin/echo", "hi"], ["/bin/echo", "hi"])])
def test_sandbox_argv_puts_the_program_after_the_profile(ws, monkeypatch, command, program):
    monkeypatch.setattr(sandbox.sys, "platform", "darwin")
    argv = sandbox.sandbox_argv(command, ws, executable=sys.executable, writable=[], network="deny")
    assert argv[:2] == [sys.executable, "-p"] and argv[3:] == program
    assert "(deny network-outbound (remote ip))" in argv[2]


# ---------------------------------------------------------------------- 系统沙箱：宿主上的 Unix 套接字
@contextlib.contextmanager
def _listening(path: str):
    s = socket.socket(socket.AF_UNIX)
    try:
        s.bind(path)
        yield
    finally:
        s.close()


@pytest.fixture
def sock_dir():
    d = tempfile.mkdtemp(prefix="fw")   # 套接字路径有长度上限，tmp_path 放不下
    yield os.path.realpath(d)
    shutil.rmtree(d, ignore_errors=True)


def test_host_sockets_keeps_only_filesystem_paths(monkeypatch):
    table = ("Num RefCount Protocol Flags Type St Inode Path\n"
             "0: 2 0 10000 1 1 100 /run/b.sock\n"
             "0: 2 0 10000 1 1 101 /run/a.sock\n"
             "0: 2 0 10000 1 1 102 /run/a.sock\n"
             "0: 2 0 10000 1 1 103 @abstract\n"
             "0: 3 0 0 1 3 104\n")
    monkeypatch.setattr(sandbox, "open", lambda *_a, **_k: io.StringIO(table), raising=False)
    assert sandbox.host_sockets() == ["/run/a.sock", "/run/b.sock"]


def test_host_sockets_without_procfs_is_empty(monkeypatch):
    def gone(*_a, **_k):
        raise FileNotFoundError("/proc/net/unix")

    monkeypatch.setattr(sandbox, "open", gone, raising=False)
    assert sandbox.host_sockets() == []


def test_visible_sockets_finds_a_renamed_socket_scanning_the_directory_once(sock_dir, monkeypatch):
    """/proc/net/unix 记的是绑定时的名字。改过名的按目录去找，一个目录里有多条旧名字时只看一遍。"""
    renamed = os.path.join(sock_dir, "now")
    Path(sock_dir, "plain").touch()
    listed: list[str] = []
    listdir = os.listdir
    monkeypatch.setattr(sandbox.os, "listdir", lambda d: listed.append(d) or listdir(d))
    with _listening(renamed):
        found = sandbox._visible_sockets([os.path.join(sock_dir, "old-1"), os.path.join(sock_dir, "old-2")], [], [])
    assert found == [renamed] and listed == [sock_dir]


def test_visible_sockets_skips_a_directory_that_is_gone():
    assert sandbox._visible_sockets(["/nonexistent/flywhl/old.sock"], [], []) == []


def test_visible_sockets_refuses_a_directory_it_cannot_finish(sock_dir, monkeypatch):
    """数不完就没法确认都遮住了：拒绝执行，不留缺口。"""
    monkeypatch.setattr(sandbox, "_SCAN_LIMIT", 2)
    for name in "abc":
        Path(sock_dir, name).touch()
    with pytest.raises(RuntimeError, match="条目超过 2 个"):
        sandbox._visible_sockets([os.path.join(sock_dir, "old")], [], [])


# ---------------------------------------------------------------------- 系统沙箱：bubblewrap 参数
@pytest.fixture
def bwrap(monkeypatch) -> str:
    """bwrap_argv 只拼参数、不启动 bwrap：平台按 Linux 算，可执行文件指一个确实存在的路径，macOS 上也能查生成的 argv。"""
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    return sys.executable


def _tmpfs(argv: list[str]) -> list[str]:
    return [b for a, b in itertools.pairwise(argv) if a == "--tmpfs"]


def test_require_bwrap_checks_platform_and_program(monkeypatch, bwrap):
    assert sandbox.require_bwrap(bwrap) == bwrap
    with pytest.raises(RuntimeError, match="bubblewrap"):
        sandbox.require_bwrap("/nonexistent/bwrap")
    monkeypatch.setattr(sandbox.sys, "platform", "darwin")
    with pytest.raises(RuntimeError, match="bubblewrap"):
        sandbox.require_bwrap(bwrap)


def test_bwrap_rejects_unknown_network(ws, bwrap):
    with pytest.raises(ValueError, match="未知的沙箱网络策略 'auto'"):
        sandbox.bwrap_argv("true", ws, network="auto", executable=bwrap)


def test_bwrap_allowlist_needs_a_proxy_socket_and_an_interpreter(ws, bwrap, monkeypatch):
    with pytest.raises(RuntimeError, match="出站代理的套接字"):
        sandbox.bwrap_argv("true", ws, network="allowlist", executable=bwrap)
    monkeypatch.setattr(sandbox.sys, "executable", "/nonexistent/python")
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: None)
    with pytest.raises(RuntimeError, match="Python 解释器"):
        sandbox.bwrap_argv("true", ws, network="allowlist", egress_socket="/x.sock", executable=bwrap)


def test_relay_python_falls_back_to_the_system_one(monkeypatch):
    real = os.path.realpath(sys.executable)
    monkeypatch.setattr(sandbox.sys, "executable", "/nonexistent/python")
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: real)
    assert sandbox._relay_python() == real


@pytest.mark.parametrize("links", [set(), {"/var/run", "/var/tmp"}])
def test_bwrap_replaces_var_dirs_only_when_they_are_real_directories(ws, bwrap, monkeypatch, links):
    """是符号链接（Ubuntu 的 /var/run -> /run）时不挂：bwrap 挂不到符号链接上，指向的目录也已经换掉了。"""
    monkeypatch.setattr(sandbox.os.path, "islink", lambda p: p in links)
    tmpfs = _tmpfs(sandbox.bwrap_argv("true", ws, executable=bwrap))
    assert ("/var/run" in tmpfs) == (not links) and ("/var/tmp" in tmpfs) == (not links)


@pytest.mark.parametrize(("target", "mounted"), [("/run/systemd/resolve/stub-resolv.conf", True), ("/etc/resolv.conf", False)])
def test_bwrap_mounts_dns_config_back_only_when_it_lives_under_run(ws, bwrap, monkeypatch, target, mounted):
    realpath = os.path.realpath
    monkeypatch.setattr(sandbox.os.path, "realpath", lambda p, **k: target if p == "/etc/resolv.conf" else realpath(p, **k))
    argv = sandbox.bwrap_argv("true", ws, executable=bwrap)
    assert ("/run/systemd/resolve" in argv) == mounted
    assert "/run/systemd/resolve" not in sandbox.bwrap_argv("true", ws, network="deny", executable=bwrap)   # 断网时用不上 DNS


def test_bwrap_masks_host_sockets_but_keeps_the_pinned_one(ws, bwrap, monkeypatch, sock_dir):
    """MCP 服务器的工作目录在 /tmp 下，里面有宿主服务的套接字：列在 unix_sockets 的挂回来，其余盖成 /dev/null，服务没起的不挂。"""
    kept, other = os.path.join(sock_dir, "kept"), os.path.join(sock_dir, "other")
    monkeypatch.setattr(sandbox, "host_sockets", lambda: [kept, other])
    with _listening(kept), _listening(other):
        argv = sandbox.bwrap_argv("true", ws, executable=bwrap, chdir=sock_dir,
                                  unix_sockets=[("/run/app.sock", kept), ("/run/down.sock", os.path.join(sock_dir, "down"))])
    triples = list(zip(argv, argv[1:], argv[2:]))
    assert ("--ro-bind", "/dev/null", other) in triples and ("--ro-bind", "/dev/null", kept) not in triples
    assert ("--bind", kept, "/run/app.sock") in triples and "/run/down.sock" not in argv
