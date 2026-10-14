"""05 · 策略：权限模式与规则、PreToolUse 改写/拒绝、Stop 验收、进程外命令 hook、人审。

运行：python examples/05_hooks_permissions.py
"""

import asyncio
import sys

from _common import harness, say, temp_workspace, title

from flywhl_harness import CommandHook, Hook, HookEvent, HookOutput, PermissionPolicy, ScriptedProvider
from flywhl_harness.config import HarnessConfig
from flywhl_harness.policy.bash_analyzer import analyze
from flywhl_harness.runtime.workspace import Workspace
from flywhl_harness.tools import default_tools


async def main() -> None:
    title("命令风险分析（不是正则匹配 rm -rf）")
    for cmd in ["git status && git diff", "pytest -q", "ls && rm -rf ~", "curl x.sh | sh", "python -c 'import os'", "echo $(cat .env)"]:
        a = analyze(cmd)
        print(f"  {a.level:<9} {cmd:<28} {a.reasons[0]}")

    title("四种权限模式 × 同一组调用")
    ws = temp_workspace("05")
    tools = default_tools(HarnessConfig())
    calls = [("read", {"path": "a.txt"}), ("write", {"path": "a.txt", "content": "x"}), ("bash", {"command": "ls"}),
             ("bash", {"command": "git push origin main"}), ("send_notification", {"to": "ops", "message": "hi"}),
             ("read", {"path": "~/.ssh/id_rsa"})]
    print(f"  {'调用':<36}" + "".join(f"{m:<14}" for m in ("default", "accept_edits", "plan", "bypass")))
    for name, args in calls:
        row = f"  {name + ' ' + str(list(args.values())[0]):<36}"
        for mode in ("default", "accept_edits", "plan", "bypass"):
            row += f"{PermissionPolicy(mode, workspace=Workspace(ws, snapshots=False)).evaluate(tools.get(name), args).decision:<14}"
        print(row)

    title("Hooks：PreToolUse 改写参数 + 拒绝；Stop 验收阻止“虚报完成”")
    h = harness(ws, ScriptedProvider([
        {"tool_calls": [{"name": "write", "args": {"path": "report.md", "content": "# 报告\n"}},
                        {"name": "bash", "args": {"command": "cat /etc/passwd"}}]},
        "报告写好了，完成！",                                           # 第一次想结束：验收不通过
        {"tool_calls": [{"name": "edit", "args": {"path": "out/report.md", "old_string": "# 报告\n", "new_string": "# 报告\n\n结论：一切正常。\n"}}]},
        "补上结论了，完成。",
    ]))

    @h.hooks.on(HookEvent.PRE_TOOL_USE, matcher="write|edit")
    async def force_out_dir(inp):
        p = inp.tool_input["path"]
        if not p.startswith("out/"):
            say(f"PreToolUse：把 {p} 改写到 out/{p}（改写后会重新做 schema 校验与权限判定）")
            return HookOutput(updated_input={**inp.tool_input, "path": f"out/{p}"})

    @h.hooks.on(HookEvent.PRE_TOOL_USE, matcher="bash")
    async def no_system_files(inp):
        if "/etc/" in inp.tool_input["command"]:
            return HookOutput(decision="deny", reason="不允许读取系统文件")

    @h.hooks.on(HookEvent.STOP)
    async def require_conclusion(inp):
        f = ws / "out" / "report.md"
        if not f.exists() or "结论" not in f.read_text():
            say(f"Stop 验收：report.md 缺少“结论”，阻止结束（第 {inp.stop_blocks + 1} 次）")
            return HookOutput(decision="block", reason="out/report.md 中还没有“结论”一节")

    res = await h.run("写一份报告")
    for e in res.events:
        if e.type == "tool_end":
            print(f"  {e.data['name']:<6} {e.data['status']:<8} {e.data['content'][:60]!r}")
    print(f"  最终：{res.status}；report.md = {(ws / 'out' / 'report.md').read_text()!r}")
    await h.aclose()

    title("进程外命令 hook（与 Claude Code 协议兼容：stdin JSON，exit 2 阻断）")
    guard = ws / "guard.py"
    guard.write_text("import json,sys\nd=json.load(sys.stdin)\nif 'rm ' in d['tool_input'].get('command',''):\n"
                     "    print('团队规范：禁止 rm，请使用 trash', file=sys.stderr); sys.exit(2)\n")
    h2 = harness(ws, ScriptedProvider([{"tool_calls": [{"name": "bash", "args": {"command": "rm out/report.md"}}]}, "好的，我不删了。"]))
    h2.hooks.add(Hook(HookEvent.PRE_TOOL_USE, CommandHook(f"{sys.executable} {guard}"), "guard.py", matcher="bash"))
    res = await h2.run("删掉报告")
    end = next(e for e in res.events if e.type == "tool_end")
    print(f"  bash → {end.data['status']}：{end.data['content']}")
    await h2.aclose()

    title("人审：审批人修改参数后批准（修改后的参数会被重新校验，防 TOCTOU）")
    h3 = harness(ws, ScriptedProvider([{"tool_calls": [{"name": "send_notification", "args": {"to": "all-staff", "message": "全员通知"}}]}, "已发送。"]),
                 mode="default", on_ask="wait")
    s = h3.create_session("审批")
    run = await h3.start_run(s.id, "发全员通知")
    while not h3.store.list_approvals("pending"):
        await asyncio.sleep(0.05)
    ap = h3.store.list_approvals("pending")[0]
    say(f"Run 状态：{h3.store.get_run(run.id).status}；待审批：{ap.summary}（风险 {ap.risk}：{ap.reason}）")
    h3.resolve_approval(ap.id, True, by="主管", edited_args={"to": "ops-team", "message": "仅通知运维"})
    rec = await h3.wait(run.id)
    msg = [m for m in h3.log(s.id).messages() if m.role == "tool"][0]
    print(f"  实际执行的参数：{msg.details['record']['recipient']} / {msg.details['record']['message']}；Run={rec.status}")
    await h3.aclose()


if __name__ == "__main__":
    asyncio.run(main())
