"""10 · 长时任务 harness：初始化会话 + 功能清单 + 进度文件 + 每会话只推进一个单元。

运行：python examples/10_long_running.py

借鉴 Anthropic《Effective harnesses for long-running agents》的做法，用 flywhl 的 hooks 实现：
- **feature_list.json**：结构化的验收清单，每项带 passes 标记；hook 保证模型 **只能修改 passes 字段**
  （write 与 edit 都按“改完之后的内容”检查；bash 里出现清单文件名的非只读命令一律拒绝；
  守卫自身出错时按拒绝处理：fail_closed）；
- **progress.md**：跨会话的进度笔记——上下文窗口之外的持久状态；
- **每个会话推进一项**：Stop hook 要求本会话至少把一项改为 passes=true、并在进度日志里追加记录后才能结束；
- **开场定位**：SessionStart hook 把“读进度 → 读清单 → 选一项”的开场流程注入给模型。
"""

import asyncio
import json

from _common import harness, say, temp_workspace, title

from flywhl_harness import HookEvent, HookOutput, ScriptedProvider
from flywhl_harness.policy.bash_analyzer import analyze

FEATURES = [
    {"id": "F1", "description": "add(a, b) 返回两数之和", "passes": False},
    {"id": "F2", "description": "divide(a, b) 在 b=0 时抛出 ValueError", "passes": False},
]


def session_script(fid: str, code: str, test: str):
    """每个“会话”：读进度 → 实现一个功能 → 跑测试 → 只把这一项标记为通过 → 写进度。"""
    return [
        {"tool_calls": [{"name": "read", "args": {"path": "progress.md"}}, {"name": "read", "args": {"path": "feature_list.json"}}]},
        {"tool_calls": [{"name": "write", "args": {"path": f"{fid}.py", "content": code}}]},
        {"tool_calls": [{"name": "bash", "args": {"command": test}}]},
        lambda req: {"tool_calls": [{"name": "write", "args": {"path": "feature_list.json", "content": _mark(req, fid)}}]},
        lambda req: {"tool_calls": [{"name": "edit", "args": {"path": "progress.md", "old_string": "## 日志\n",
                                                               "new_string": f"## 日志\n- {fid} 完成并通过测试\n"}}]},
        f"{fid} 完成：实现 + 测试 + 清单 + 进度都已更新。",
    ]


def _mark(req, fid):
    data = json.loads(open(WS / "feature_list.json").read())
    for f in data:
        if f["id"] == fid:
            f["passes"] = True
    return json.dumps(data, ensure_ascii=False, indent=2)


WS = temp_workspace("10")


def passes() -> int:
    return sum(f["passes"] for f in json.loads((WS / "feature_list.json").read_text()))


def log_lines() -> int:
    return sum(1 for line in (WS / "progress.md").read_text().splitlines() if line.startswith("- "))


def install_hooks(h, *, coding_session: bool = True):
    baseline: dict[str, tuple[int, int]] = {}
    target = (WS / "feature_list.json").resolve()

    @h.hooks.on(HookEvent.SESSION_START)
    async def orient(inp):
        baseline[inp.session_id] = (passes(), log_lines())  # 会话开始时的基线，供 Stop 验收比较
        return HookOutput(additional_context="开场流程：先读 progress.md 与 feature_list.json，选一个 passes=false 的最高优先级功能，只做这一项。")

    # fail_closed：守卫自己出错（例如清单文件损坏）时按拒绝处理——守卫失效不能等于放行
    @h.hooks.on(HookEvent.PRE_TOOL_USE, matcher="write|edit|bash", fail_closed=True)
    async def guard_feature_list(inp):
        if inp.tool_name == "bash":
            # 用 sed -i / perl -pi / 重定向改清单绕过 write 检查：只读命令（cat、jq）放行，其余一律拒绝
            cmd = str(inp.tool_input.get("command", ""))
            if target.name in cmd and analyze(cmd).level != "read":
                return HookOutput(decision="deny", reason="feature_list.json 只能用 write / edit 修改（以便逐项检查）")
            return None
        # 按解析后的真实路径比较：./feature_list.json、子目录/../feature_list.json 都指向同一个文件
        if (WS / str(inp.tool_input.get("path", ""))).resolve() != target:
            return None
        current = target.read_text()
        if inp.tool_name == "edit":  # 先算出“改完之后”的内容，再和 write 走同一套检查
            old_s = inp.tool_input.get("old_string", "")
            if not old_s or old_s not in current:
                return None  # 交给 edit 工具自己报“找不到 old_string”
            new_s = inp.tool_input.get("new_string", "")
            after = current.replace(old_s, new_s) if inp.tool_input.get("replace_all") else current.replace(old_s, new_s, 1)
        else:
            after = inp.tool_input.get("content", "")
        try:
            new = json.loads(after)
        except json.JSONDecodeError:
            return HookOutput(decision="deny", reason="修改后的 feature_list.json 不是合法 JSON")
        strip = lambda items: [{k: v for k, v in f.items() if k != "passes"} for f in items]  # noqa: E731
        if strip(json.loads(current)) != strip(new):
            return HookOutput(decision="deny", reason="只允许修改 passes 字段，不得增删或改写功能描述")

    if not coding_session:
        return

    @h.hooks.on(HookEvent.STOP)
    async def one_unit_done(inp):
        p0, l0 = baseline.get(inp.session_id, (0, 0))
        if passes() <= p0:
            return HookOutput(decision="block", reason="本会话还没有把任何功能标记为通过（需先实现并跑通测试）")
        if log_lines() <= l0:
            return HookOutput(decision="block", reason="请先在 progress.md 的日志里记录本会话的进展，再结束")


async def main() -> None:
    title("10 长时任务：初始化")
    (WS / "feature_list.json").write_text(json.dumps(FEATURES, ensure_ascii=False, indent=2))
    (WS / "progress.md").write_text("# 进度\n\n## 日志\n")
    say("初始化 Agent 生成了：功能清单（全部 passes=false）+ 进度文件")

    sessions = [
        ("F1", "def add(a, b):\n    return a + b\n", "python3 -c \"from F1 import add; assert add(1, 2) == 3; print('F1 ok')\""),
        ("F2", "def divide(a, b):\n    if b == 0:\n        raise ValueError('b 不能为 0')\n    return a / b\n",
         "python3 -c \"from F2 import divide\ntry:\n    divide(1, 0)\nexcept ValueError:\n    print('F2 ok')\""),
    ]
    for i, (fid, code, test) in enumerate(sessions, 1):
        title(f"第 {i} 个编码会话（全新上下文）")
        h = harness(WS, ScriptedProvider(session_script(fid, code, test)))
        install_hooks(h)
        s = h.create_session(f"会话 {i}")
        run = await h.start_run(s.id, "继续推进项目")
        rec = await h.wait(run.id)
        for e in h.store.list_events(run.id):
            if e.type == "tool_end":
                print(f"  {e.data['name']:<6} {e.data['status']:<7} {e.data['content'][:70]!r}")
        say(f"会话 {i} 结束：{rec.status}")
        await h.aclose()

    title("跨会话的持久状态")
    print((WS / "progress.md").read_text())
    print(json.dumps(json.loads((WS / "feature_list.json").read_text()), ensure_ascii=False))

    title("尝试篡改功能清单（被 hook 拒绝）")
    before = (WS / "feature_list.json").read_text()
    bad = [{"id": "F1", "description": "（被删掉了）", "passes": True}]
    h = harness(WS, ScriptedProvider([
        {"tool_calls": [{"name": "write", "args": {"path": "./feature_list.json", "content": json.dumps(bad, ensure_ascii=False)}},
                        {"name": "edit", "args": {"path": "feature_list.json", "old_string": "add(a, b) 返回两数之和",
                                                  "new_string": "（随便写写）"}}]},
        {"tool_calls": [{"name": "bash", "args": {"command": "sed -i.bak 's/F2/F9/' feature_list.json"}}]},
        "好的，清单保持不变。"]), mode="bypass")  # 即使在 bypass 模式下，hook 守卫依然生效
    install_hooks(h, coding_session=False)  # 这不是编码会话：不要求推进功能
    res = await h.run("把清单简化一下")
    for end in (e for e in res.events if e.type == "tool_end"):
        print(f"  {end.data['name']:<5} → {end.data['status']}：{end.data['content'][:60]}")
    say(f"运行结束：{res.status}；清单逐字未变：{(WS / 'feature_list.json').read_text() == before}")
    await h.aclose()


if __name__ == "__main__":
    asyncio.run(main())
