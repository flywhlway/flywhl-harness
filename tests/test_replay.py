"""确定性回放：录制时从外部进来的东西（模型写的摘要、人的插话、恢复说明、验收命令的结果）都要从轨迹里取。

脚本化模型的压缩摘要恰好等于回放用的确定性摘要，所以只用它测不出压缩分叉；这里的 ``WrittenSummary`` 和真实模型一样另写一份摘要。
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from flywhl_harness import ScriptedProvider, Store
from flywhl_harness.llm.base import StreamEvent
from flywhl_harness.observe.replay import replay_run, replay_trajectory
from flywhl_harness.observe.trace import dumps, export_trajectory
from flywhl_harness.types import Message, MsgKind, TextBlock

from .conftest import make_harness


class WrittenSummary(ScriptedProvider):
    async def stream(self, req, cancel):
        if req.purpose != "compaction":
            async for ev in super().stream(req, cancel):
                yield ev
            return
        yield StreamEvent("start")
        yield StreamEvent("done", message=Message(role="assistant", content=[TextBlock("模型自己写的摘要：任务是打印很多东西。")],
                                                  stop_reason="end_turn"))


def call(name: str, **args) -> dict:
    return {"tool_calls": [{"name": name, "args": args}]}


def bash(command: str) -> dict:
    return call("bash", command=command)


PRINTS = [bash(f"python3 -c \"print('{i}' * 3000)\"") for i in range(8)]
TIGHT = dict(context__context_window=8000, context__keep_recent_tokens=1500, context__reserve_tokens=1000,
             loop_guard__stop_repeats=99, context__clear_old_tool_results=False)


def assert_same(rep) -> None:
    assert rep.divergence is None, rep.notes
    assert rep.same_tool_sequence, (rep.original_tools, rep.replay_tools)
    assert rep.same_final_text and rep.status == rep.original_status
    assert rep.deterministic


async def test_compaction_summary_comes_from_the_recording(ws):
    h = make_harness(ws, WrittenSummary([*PRINTS, "完成"], mode="sequential"), **TIGHT)
    res = await h.run("打印很多东西")
    assert res.status == "completed"
    assert export_trajectory(h.store, res.run_id)["compactions"], "这条用例要真的触发压缩"
    assert_same(await replay_run(h.store, res.run_id, config=h.config, base_tools=h.tools))


async def test_steer_is_replayed_at_the_same_point(ws):
    (ws / "a.txt").write_text("1\n")
    h = make_harness(ws, [bash("sleep 0.3"), call("read", path="a.txt"), "好"])
    s = h.create_session("t")
    run = await h.start_run(s.id, "开始")
    await asyncio.sleep(0.05)
    assert h.steer(run.id, "另外也看看 a.txt")
    assert (await h.wait(run.id)).status == "completed"
    assert_same(await replay_run(h.store, run.id, config=h.config, base_tools=h.tools))


async def test_follow_up_is_replayed_when_the_run_would_stop(ws):
    h = make_harness(ws, [bash("sleep 0.3"), "第一件做完了", "第二件也做完了"])
    s = h.create_session("t")
    run = await h.start_run(s.id, "开始")
    await asyncio.sleep(0.05)
    assert h.follow_up(run.id, "做完再说一声")
    assert (await h.wait(run.id)).status == "completed"
    assert_same(await replay_run(h.store, run.id, config=h.config, base_tools=h.tools))


async def test_paused_and_resumed_run_replays(ws):
    h = make_harness(ws, [bash("sleep 0.3"), call("ls"), "好"])
    s = h.create_session("t")
    run = await h.start_run(s.id, "开始")
    await asyncio.sleep(0.05)
    await h.pause(run.id)
    assert (await h.wait(run.id)).status == "paused"
    await h.resume(run.id)
    assert (await h.wait(run.id)).status == "completed"
    msgs = h.log(s.id).messages()
    assert any(m.kind == MsgKind.RESUME_NOTE for m in msgs), "这条用例要真的走恢复路径"
    assert_same(await replay_run(h.store, run.id, config=h.config, base_tools=h.tools))


async def test_verify_command_is_not_rerun_on_replay(ws):
    """工作区是终态，重新跑验收会直接通过，回放就比录制少走几步；验收的结果要从轨迹里取。"""
    h = make_harness(ws, [call("write", path="d.txt", content="x"), "做完了", call("write", path="ok", content="1"), "这回真做完了"],
                     verify_command="test -f ok")
    res = await h.run("开始")
    assert res.status == "completed"
    assert_same(await replay_run(h.store, res.run_id, config=h.config, base_tools=h.tools))


async def test_later_run_in_a_session_replays_with_its_history(ws):
    (ws / "a.txt").write_text("1\n")
    h = make_harness(ws, [call("read", path="a.txt"), "第一轮完成", call("ls"), "第二轮完成"])
    first = await h.run("先读文件")
    second = await h.run("再列目录", session_id=first.session_id)
    assert second.status == "completed"
    assert_same(await replay_run(h.store, second.run_id, config=h.config, base_tools=h.tools))


async def test_history_keeps_the_compaction_point(ws):
    h = make_harness(ws, WrittenSummary([*PRINTS, "第一轮完成", call("ls"), "第二轮完成"], mode="sequential"), **TIGHT)
    first = await h.run("打印很多东西")
    second = await h.run("再列目录", session_id=first.session_id)
    assert export_trajectory(h.store, first.run_id)["compactions"]
    assert_same(await replay_run(h.store, second.run_id, config=h.config, base_tools=h.tools))


async def test_cancelled_run_replays_up_to_where_it_stopped(ws):
    h = make_harness(ws, [call("ls"), bash("sleep 5"), "不会走到这里"])
    s = h.create_session("t")
    run = await h.start_run(s.id, "开始")
    await asyncio.sleep(0.3)
    await h.cancel(run.id)
    assert (await h.wait(run.id)).status == "cancelled"
    rep = await replay_run(h.store, run.id, config=h.config, base_tools=h.tools)
    assert rep.divergence is None and rep.same_tool_sequence
    assert rep.original_status == "cancelled" and rep.deterministic


async def test_completed_run_with_a_changed_tool_sequence_is_not_deterministic(ws):
    (ws / "a.txt").write_text("1\n")
    h = make_harness(ws, [call("read", path="a.txt"), "好"])
    res = await h.run("读")
    traj = json.loads(dumps(export_trajectory(h.store, res.run_id)))
    traj["fingerprints"][1] = "别的指纹"
    rep = await replay_trajectory(traj, config=h.config, base_tools=h.tools)
    assert rep.divergence is not None and not rep.deterministic


async def test_replays_from_a_saved_trajectory_without_config_or_workspace(ws):
    """基准的原始数据是导出的 JSONL：工作区早就删了，运行时的配置也不在手边，回放只靠轨迹本身。"""
    (ws / "a.txt").write_text("1\n")
    h = make_harness(ws, [call("ls"), call("ls"), call("ls"), "收尾"], budget__max_turns=2,
                     context__context_window=50_000, loop_guard__stop_repeats=99)
    res = await h.run("开始")
    assert res.status == "budget_exhausted"
    saved = dumps(export_trajectory(h.store, res.run_id))
    await h.aclose()
    shutil.rmtree(ws)
    assert_same(await replay_trajectory(json.loads(saved)))


async def test_plan_mode_todos_do_not_block_the_replay(ws):
    """规划模式的计划项本来就没做完，录制时不阻止结束。回放的模式是 bypass，不能因此多出验收反馈。"""
    todos = [{"content": "写报告", "status": "pending"}]
    h = make_harness(ws, [call("todo_write", todos=todos), "计划写好了"], permissions__mode="plan")
    res = await h.run("做个计划")
    assert res.status == "completed"
    assert_same(await replay_run(h.store, res.run_id, config=h.config, base_tools=h.tools))


async def test_recording_keeps_only_what_replay_needs(ws):
    h = make_harness(ws, ["好"], verify_command="echo 不该出现在轨迹里")
    res = await h.run("开始")
    settings = h.store.get_run(res.run_id).config["replay"]
    assert settings["verify"] is True and settings["context_window"] == h.model_info.context_window
    assert "不该出现在轨迹里" not in json.dumps(settings, ensure_ascii=False)


async def test_changed_context_with_the_same_tool_sequence_is_reported_as_divergence(ws):
    """回归：模型输出取自录制，工具序列和最终答案几乎总相同，分叉只能靠上下文指纹发现。

    页面曾经只看 same_tool_sequence 和 same_final_text，这种情况会报“回放一致”。
    """
    h = make_harness(ws, [call("ls", path="."), "看完了。"])
    res = await h.run("看看目录")
    traj = json.loads(dumps(export_trajectory(h.store, res.run_id)))
    traj["session"]["system_prompt"] += "\n（改过的系统提示词）"
    rep = await replay_trajectory(traj, keep_workspace=False)
    assert rep.same_tool_sequence and rep.same_final_text
    assert rep.divergence["step"] == 0 and not rep.deterministic
    assert rep.notes == ["第 1 次模型调用的上下文与录制时不一致：harness 行为发生了变化"]


async def test_workspace_copy_is_removed_when_not_kept_or_when_replay_fails(ws, tmp_path, monkeypatch):
    """回放在工作区的临时副本里进行。页面上的回放不需要副本；出错时调用方拿不到路径，也必须当场删掉。"""
    import tempfile

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    (ws / "a.txt").write_text("1\n")
    h = make_harness(ws, [call("ls", path="."), "看完了。"])
    res = await h.run("看看目录")
    traj = json.loads(dumps(export_trajectory(h.store, res.run_id)))

    kept = await replay_trajectory(traj)
    assert kept.workspace and (Path(kept.workspace) / "a.txt").is_file(), "默认保留副本，CLI 与基准据此检查文件"
    shutil.rmtree(kept.workspace)

    rep = await replay_trajectory(traj, keep_workspace=False)
    assert rep.deterministic and rep.workspace == ""
    assert not list(scratch.iterdir())

    broken = {**traj, "session": {**traj["session"], "tools": 5}}   # 复制完工作区之后才会出错
    with pytest.raises(TypeError):
        await replay_trajectory(broken)
    assert not list(scratch.iterdir())


async def test_replay_endpoint_returns_the_report_and_leaves_no_copy(ws, tmp_path, monkeypatch):
    import tempfile

    import httpx

    from flywhl_harness.server.app import create_app

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    h = make_harness(ws, [call("ls", path="."), "看完了。"])
    res = await h.run("看看目录")
    app = create_app(h, auto_recover=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765") as c:
        r = await c.post(f"/api/runs/{res.run_id}/replay", json={})
    assert r.status_code == 200
    rep = r.json()
    assert rep["deterministic"] is True and rep["original_tools"] == rep["replay_tools"] == ["ls"]
    assert rep["workspace"] == "" and not list(scratch.iterdir())


# ---------------------------------------------------------------------- 联网工具
async def _web_run(ws):
    import httpx

    from .webkit import DS_FETCH, DS_SEARCH, Net, ds_search, fetch_call, fixture, make_web_harness, search_call

    net = Net().on("POST", DS_SEARCH, ds_search()).on("POST", DS_FETCH, httpx.Response(200, json=fixture("deepseek", "fetch")))
    h = make_web_harness(ws, [search_call("httpx"), fetch_call("https://pypi.org/project/httpx/"), "答案"], net,
                         web__default="fetch")
    res = await h.run("httpx 最新版本")
    assert res.status == "completed"
    return h, net, res, search_call


async def test_web_runs_replay_deterministically_without_touching_the_network(ws):
    from flywhl_harness import HarnessConfig

    h, net, res, _ = await _web_run(ws)
    sent = len(net.requests)
    assert_same(await replay_run(h.store, res.run_id, config=h.config, base_tools=h.tools))
    # 回放的机器没开联网：工具规格是静态的，从录制重建，指纹照样一致
    assert_same(await replay_run(h.store, res.run_id, config=HarnessConfig.load(workspace=str(ws))))
    assert len(net.requests) == sent
    rec = export_trajectory(h.store, res.run_id)
    assert rec["run"]["config"]["replay"]["web"] == {"capability": "fetch", "max_searches_per_run": 20,
                                                     "max_fetches_per_run": 10}


def _echo_last_tool(req):
    return next(m.text for m in reversed(req.messages) if m.role == "tool")


def _live_search(query: str) -> dict:
    # 显式 id：脚本模型按请求序号编号，分叉后会和录制里的编号撞上（真实模型的编号不会）
    return {"tool_calls": [{"id": "live_1", "name": "web_search", "args": {"query": query}}]}


async def test_forked_web_calls_dry_run_unless_recorded_or_allowed(ws):
    h, net, res, _ = await _web_run(ws)
    sent = len(net.requests)
    new = ScriptedProvider(["-", _live_search("别的查询"), _echo_last_tool])
    rep = await replay_run(h.store, res.run_id, config=h.config, base_tools=h.tools, live=new, fork_at=1)
    assert rep.final_text.startswith("[dry-run] web_search") and "--allow-web" in rep.final_text
    assert len(net.requests) == sent

    same = ScriptedProvider(["-", _live_search("httpx"), _echo_last_tool])   # 与录制同参数：复用录制结果
    rep = await replay_run(h.store, res.run_id, config=h.config, base_tools=h.tools, live=same, fork_at=1)
    assert rep.final_text.startswith("[联网搜索结果") and len(net.requests) == sent

    allowed = ScriptedProvider(["-", _live_search("别的查询"), _echo_last_tool])
    rep = await replay_run(h.store, res.run_id, config=h.config, base_tools=h.tools, live=allowed, fork_at=1, allow_web=True)
    assert rep.final_text.startswith("[联网搜索结果") and len(net.requests) == sent + 1
