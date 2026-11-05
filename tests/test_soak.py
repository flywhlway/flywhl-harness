"""scripts/soak.py 的离线用例：只用合成数据测判定逻辑和安全检查，不启动任何进程。"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def soak():
    spec = importlib.util.spec_from_file_location("soak", ROOT / "scripts" / "soak.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["soak"] = module
    spec.loader.exec_module(module)
    yield module
    del sys.modules["soak"]


def rss_samples(slope_mib_per_s: float, n: int = 40, step: float = 30.0) -> list[dict]:
    return [{"t": i * step, "rss_mib": 100 + slope_mib_per_s * i * step} for i in range(n)]


def test_rss_flat_passes(soak):
    verdict = soak.rss_verdict(rss_samples(0.0), 64)
    assert verdict["passed"] and verdict["growth_mib_per_2h"] == 0


def test_rss_growth_is_scaled_to_two_hours(soak):
    # 每秒 0.01 MiB，2 小时 72 MiB
    verdict = soak.rss_verdict(rss_samples(0.01), 64)
    assert not verdict["passed"] and verdict["growth_mib_per_2h"] == pytest.approx(72)
    assert soak.rss_verdict(rss_samples(0.01), 80)["passed"]


def test_rss_warmup_is_skipped(soak):
    # 前 20% 猛涨，之后走平：只算预热之后，不该判失败
    samples = [{"t": i * 30.0, "rss_mib": 500.0 if i >= 8 else 100.0 + i * 50} for i in range(40)]
    assert soak.rss_verdict(samples, 64)["passed"]


def test_too_few_samples_fail_instead_of_passing(soak):
    samples = [{"t": float(i), "rss_mib": 100.0, "fds": 10} for i in range(3)]
    assert not soak.rss_verdict(samples, 64)["passed"]
    assert not soak.fd_verdict(samples, 8)["passed"]


def fd_samples(head: int, tail: int) -> list[dict]:
    warm = [{"t": float(i), "fds": 500} for i in range(10)]     # 预热期的数字不参与判定
    return warm + [{"t": 10.0 + i, "fds": head if i < 20 else tail} for i in range(40)]


def test_fd_growth_limit(soak):
    assert soak.fd_verdict(fd_samples(20, 28), 8)["passed"]     # 正好多 8 个不算超
    verdict = soak.fd_verdict(fd_samples(20, 29), 8)
    assert not verdict["passed"] and verdict["growth"] == 9


def test_fd_single_spike_does_not_fail(soak):
    samples = fd_samples(20, 20)
    samples[-1]["fds"] = 200    # 采样正好撞上临时打开的管道
    assert soak.fd_verdict(samples, 8)["passed"]


def test_seq_problem(soak):
    assert soak.seq_problem([1, 2, 3]) is None
    assert "跳" in soak.seq_problem([1, 2, 4])
    assert "跳" in soak.seq_problem([1, 2, 2])
    assert "开始" in soak.seq_problem([2, 3])
    assert soak.seq_problem([]) == "没有收到事件"


def sse_client(body: str) -> httpx.AsyncClient:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=body))
    return httpx.AsyncClient(transport=transport, base_url="http://soak.test")


def test_read_events_collects_seqs_and_tool_results(soak):
    body = ("retry: 1500\n\n"
            "id: 1\nevent: status\ndata: {}\n\n"
            ": ping\n\n"
            'id: 2\nevent: tool_end\ndata: {"data": {"name": "bash", "is_error": false}}\n\n'
            "event: stream_end\ndata: {}\n\n")
    seqs, tools = asyncio.run(soak.read_events(sse_client(body), "r_1"))
    assert seqs == [1, 2]
    assert tools == [{"name": "bash", "is_error": False}]


def test_read_events_without_stream_end_is_an_interruption(soak):
    with pytest.raises(ConnectionError):
        asyncio.run(soak.read_events(sse_client("id: 1\nevent: status\ndata: {}\n\n"), "r_1"))


def test_serve_cmd_keeps_token_starting_with_dash_in_one_argument(soak, tmp_path):
    # 分开写的话 argparse 把 -abc 当成选项，服务以退出码 2 起不来，大约 1/64 的令牌会撞上
    cmd = soak.serve_cmd(tmp_path, tmp_path / "flywhl.toml", 8765, "-abc")
    assert "--token=-abc" in cmd and "--token" not in cmd


def test_prepare_writes_durability_only_when_set(soak, tmp_path):
    for name, durability, present in (("a", "normal", True), ("b", "default", False)):
        base = tmp_path / name
        base.mkdir()
        assert ('durability = "normal"' in soak.prepare(base, durability).read_text(encoding="utf-8")) is present


def fake_api(calls: list) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"id": f"s{len(calls)}"})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test")


@pytest.mark.parametrize(("rotate_every", "want"), [(0, ["s1"] * 6), (3, ["s1", "s1", "s2", "s2", "s2", "s3"])])
async def test_session_loop_changes_sessions_only_when_asked(soak, tmp_path, monkeypatch, rotate_every, want):
    tally, seen = soak.Tally(), []

    async def one_run(client, sid, command):
        seen.append(sid)
        if command.startswith("touch "):
            (tmp_path / command.split()[1]).touch()
        if len(seen) == 6:
            tally.failed = soak.MAX_FAILURES     # 只是为了让循环退出
        return 0.1, 3, []

    monkeypatch.setattr(soak, "one_run", one_run)
    async with fake_api([]) as client:
        await soak.session_loop(client, 0, soak.time.monotonic() + 30, tally, tmp_path, rotate_every)
    assert seen == want and tally.sessions == len(set(want))


async def test_ping_loop_records_latency_without_failing_the_run(soak):
    tally, calls = soak.Tally(), []
    async with fake_api(calls) as client:
        await soak.ping_loop(client, soak.time.monotonic() + 0.05, tally, every=0.01)
    assert calls and len(tally.pings) == len(calls) and tally.failed == 0
    assert soak.latency(tally.pings)["p50_s"] is not None


def test_ledger_exactly_once_passes(soak):
    verdict = soak.ledger_verdict(["a\tcycle-0\t1\n", "b\tcycle-1\t2\n"], ["a", "b"])
    assert verdict["passed"] and verdict["lines"] == 2


def test_ledger_duplicate_fails(soak):
    verdict = soak.ledger_verdict(["a\tcycle-0\t1\n", "a\tcycle-0\t2\n", "b\tcycle-1\t3\n"], ["a", "b"])
    assert not verdict["passed"] and verdict["duplicated"] == ["a"] and not verdict["missing"]


def test_ledger_missing_fails(soak):
    verdict = soak.ledger_verdict(["a\tcycle-0\t1\n"], ["a", "b"])
    assert not verdict["passed"] and verdict["missing"] == ["b"] and not verdict["duplicated"]


def test_ledger_unknown_key_fails(soak):
    verdict = soak.ledger_verdict(["a\tcycle-0\t1\n", "z\tcycle-9\t2\n"], ["a"])
    assert not verdict["passed"] and verdict["unknown"] == ["z"]


def test_kill_phase(soak):
    assert soak.kill_phase(None) == "before_intent"
    assert soak.kill_phase("started") == "between_intent_and_result"
    assert soak.kill_phase("ok") == "after_result"


def test_cycle_problems(soak):
    assert soak.cycle_problems("completed", [1, 2, 3], 1, True, "w1", "w2") == []
    assert soak.cycle_problems("completed", [1, 2], 1, False, "w1", "w1") == []     # 没接管就不比较 worker
    assert any("failed" in p for p in soak.cycle_problems("failed", [1], 1, True, "w1", "w2"))
    assert any("单调" in p for p in soak.cycle_problems("completed", [1, 3, 2], 1, True, "w1", "w2"))
    assert any("单调" in p for p in soak.cycle_problems("completed", [1, 1], 1, True, "w1", "w2"))
    assert any("步骤" in p for p in soak.cycle_problems("completed", [1, 2], 2, True, "w1", "w2"))
    assert any("接管者" in p for p in soak.cycle_problems("completed", [1, 2], 1, True, "w1", "w1"))


def test_clean_up_refuses_wrong_prefix(soak):
    victim = Path(tempfile.mkdtemp(prefix="not-soak-")).resolve()
    try:
        with pytest.raises(ValueError):
            soak.clean_up(victim)
        assert victim.is_dir()
    finally:
        victim.rmdir()


def test_clean_up_refuses_outside_system_temp(soak, tmp_path):
    victim = tmp_path / f"{soak.PREFIX}outside"
    victim.mkdir()
    with pytest.raises(ValueError):
        soak.clean_up(victim)
    assert victim.is_dir()


def test_clean_up_refuses_symlink(soak):
    target = Path(tempfile.mkdtemp(prefix="not-soak-")).resolve()
    link = target.parent / f"{soak.PREFIX}link"
    link.symlink_to(target)
    try:
        with pytest.raises(ValueError):
            soak.clean_up(link)
        assert target.is_dir()
    finally:
        link.unlink()
        target.rmdir()


def test_clean_up_removes_own_base(soak):
    base = soak.make_base()
    (base / "marker").touch()
    soak.clean_up(base)
    assert not base.exists()


class FakeProc:
    def __init__(self, pid: int, returncode: int | None = None):
        self.pid, self.returncode = pid, returncode

    def poll(self) -> int | None:
        return self.returncode


@pytest.fixture
def no_real_signal(monkeypatch):
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)))
    return sent


def test_kill9_refuses_process_it_did_not_start(soak, no_real_signal):
    with pytest.raises(RuntimeError):
        soak.kill9(FakeProc(os.getpid()))
    assert no_real_signal == []


def test_kill9_skips_exited_child(soak, no_real_signal, monkeypatch):
    proc = FakeProc(424242, returncode=0)
    monkeypatch.setitem(soak._spawned, proc.pid, proc)
    assert soak.kill9(proc) is False
    assert no_real_signal == []


def test_kill9_refuses_when_no_longer_a_child(soak, no_real_signal, monkeypatch):
    proc = FakeProc(424242)
    monkeypatch.setitem(soak._spawned, proc.pid, proc)
    monkeypatch.setattr(soak, "is_own_child", lambda pid: False)
    with pytest.raises(RuntimeError):
        soak.kill9(proc)
    assert no_real_signal == []


def test_kill9_signals_own_child(soak, no_real_signal, monkeypatch):
    proc = FakeProc(424242)
    monkeypatch.setitem(soak._spawned, proc.pid, proc)
    monkeypatch.setattr(soak, "is_own_child", lambda pid: True)
    assert soak.kill9(proc) is True
    assert no_real_signal == [(424242, soak.signal.SIGKILL)]
