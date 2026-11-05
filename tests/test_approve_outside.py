"""--approve-outside：无人值守时按命令模式放行离开沙箱的 bash 命令。

命令离开沙箱就没有东西兜底，这里的用例大多在证明“放行不了”。会真正执行的只有 touch 哨兵文件；
git push 这些字符串都在判定阶段被拒绝，不会运行。
"""

from __future__ import annotations

import json
from argparse import Namespace

import pytest

from flywhl_harness import cli
from flywhl_harness.config import HarnessConfig
from flywhl_harness.events import E
from flywhl_harness.observe.replay import replay_trajectory
from flywhl_harness.observe.trace import dumps, export_trajectory
from flywhl_harness.policy.approvals import OUTSIDE_BY, ApprovalDecision
from flywhl_harness.policy.bash_analyzer import analyze
from flywhl_harness.policy.hooks import Hook, HookEvent, HookOutput
from flywhl_harness.policy.permissions import PermissionPolicy, outside_rule
from flywhl_harness.tools.confine import Confinement
from flywhl_harness.tools.shell import BashTool

from .conftest import make_harness, tool_ends

BOX = dict(permissions__mode="bypass", permissions__sandbox="macos")


def bash(command: str, **args) -> dict:
    return {"tool_calls": [{"name": "bash", "args": {"command": command, **args}}]}


def outside(command: str) -> dict:
    return bash(command, outside_sandbox=True)


def harness(ws, script, *patterns, on_ask="deny", **over):
    h = make_harness(ws, script, on_ask=on_ask, **{**BOX, **over})
    h.approve_outside = [outside_rule(p) for p in patterns]
    return h


def boxed() -> BashTool:
    t = BashTool(sandbox="off")
    t.confinement = Confinement("macos", "allowlist")
    return t


# ---------------------------------------------------------------- 模式的写法
@pytest.mark.parametrize("pattern", ["git push origin*", "git push origin main", "gh pr create*", "git *"])
def test_a_command_pattern_starts_with_a_concrete_command(pattern):
    assert outside_rule(pattern).pattern == pattern


@pytest.mark.parametrize("pattern", ["", "  ", "*", "**", "* *", "?it push", "git*", "[g]it push", "./deploy.sh", "/usr/bin/git push*",
                                     "bash(git push*)", "bash *", "sh deploy.sh", "zsh*", "python deploy.py", "python3 *", "node x.js",
                                     "env *", "sudo git push", "xargs *", "eval *", "npx *", "time git push", "nohup git push"])
def test_patterns_that_open_everything_are_refused(pattern):
    with pytest.raises(ValueError):
        outside_rule(pattern)


def test_the_setting_cannot_come_from_a_config_file(tmp_path):
    (tmp_path / "c.toml").write_text('[permissions]\napprove_outside = ["git push*"]\n', encoding="utf-8")
    with pytest.raises(ValueError, match="approve_outside"):
        HarnessConfig.load(str(tmp_path / "c.toml"), workspace=str(tmp_path))


# ---------------------------------------------------------------- 放行
async def test_a_matching_command_runs_outside_when_unattended(ws):
    h = harness(ws, [outside("touch ran.txt"), "好"], "touch ran*")
    res = await h.run("跑")
    end = tool_ends(res.events)[0]
    assert (ws / "ran.txt").exists() and end["details"]["outside_sandbox"] is True


async def test_each_release_is_listed_in_the_audit_trail(ws):
    h = harness(ws, [outside("touch ran.txt"), "好"], "touch ran*")
    res = await h.run("跑")
    resolved = [e.data for e in res.events if e.type == E.APPROVAL_RESOLVED]
    assert [(r["approved"], r["by"]) for r in resolved] == [(True, OUTSIDE_BY)]
    notice = next(e.data for e in res.events if e.type == E.NOTICE and e.data.get("code") == "approve_outside")
    assert "touch ran.txt" in notice["text"] and "touch ran*" in notice["text"]
    records = h.store.list_approvals(status="approved", run_id=res.run_id)
    assert [(r.tool, r.decided_by, r.args["command"]) for r in records] == [("bash", OUTSIDE_BY, "touch ran.txt")]
    assert "touch ran*" in records[0].decision_reason


async def test_replay_does_not_run_the_command_again(ws):
    h = harness(ws, [outside("touch ran.txt"), "好"], "touch ran*")
    res = await h.run("跑")
    (ws / "ran.txt").unlink()
    report = await replay_trajectory(json.loads(dumps(export_trajectory(h.store, res.run_id))))
    assert report.deterministic and not (ws / "ran.txt").exists(), report.notes


# ---------------------------------------------------------------- 有人在场、--yes
async def test_a_person_in_the_room_is_still_asked(ws):
    h = harness(ws, [outside("touch ran.txt"), "好"], "touch ran*", on_ask="wait")
    asked = []

    async def no(rec):
        asked.append(rec.args["command"])
        return ApprovalDecision(False, by="user", reason="不行")

    h.approvals.resolver = no
    await h.run("跑")
    assert asked == ["touch ran.txt"] and not (ws / "ran.txt").exists()


async def test_yes_does_not_turn_it_on(ws):
    h = harness(ws, [outside("touch ran.txt"), "好"], "touch ran*", on_ask="allow")
    end = tool_ends((await h.run("跑")).events)[0]
    assert "无人值守时一律拒绝" in end["content"] and not (ws / "ran.txt").exists()


# ---------------------------------------------------------------- 匹配之外的都拒绝
@pytest.mark.parametrize("command", ["touch other.txt", "touch ran.txt && touch other.txt", "touch ran.txt; touch other.txt",
                                     "touch ran.txt\ntouch other.txt", "touch ran.txt | tee other.txt", "touch ran.txt & touch other.txt"])
async def test_every_segment_has_to_match(ws, command):
    h = harness(ws, [outside(command), "好"], "touch ran*")
    end = tool_ends((await h.run("跑")).events)[0]
    assert "没有匹配的 --approve-outside 模式" in end["content"]
    assert not (ws / "ran.txt").exists() and not (ws / "other.txt").exists()


@pytest.mark.parametrize(("command", "why"), [
    ("git push $(cat target)", "不在命令文本里"),
    ("git push `cat target`", "不在命令文本里"),
    ("git push <(cat target)", "不在命令文本里"),
    ("git push --receive-pack=sentinel origin main", "不在命令文本里"),
    ("git push --exec=sentinel origin main", "不在命令文本里"),
    ("git push 'ext::sentinel' main", "不在命令文本里"),
    ("git push --rece=sentinel origin main", "不在命令文本里"),
    ("git push --repo=ext::sentinel main", "不在命令文本里"),
    ("git push origin main; touch sentinel; echo \\", "不在命令文本里"),   # shlex 解析失败，bash 照样执行分号后面的命令
    ("git push -qf origin main", "高危"),
    ("git push --mirror origin", "高危"),
    ("git push origin :main", "高危"),
    ("git push --force origin main", "高危"),
    ("git push origin +main", "高危"),
    ("git push > notes.txt", "重定向"),
    ("git push >> ../outside.txt", "重定向"),
    ("git push &> notes.txt", "重定向"),
])
def test_commands_the_pattern_cannot_vouch_for_are_refused(ws, command, why):
    from flywhl_harness.runtime.workspace import Workspace

    policy = PermissionPolicy("default", workspace=Workspace(ws), approve_outside=[outside_rule("git push*")])
    tool, args = boxed(), {"command": command, "outside_sandbox": True}
    perm = policy.evaluate(tool, args)
    assert perm.human_only
    rule, reason = policy.outside_approval(tool, args, perm)
    assert rule is None and why in reason


def test_stderr_redirection_is_not_a_file_write(ws):
    from flywhl_harness.runtime.workspace import Workspace

    policy = PermissionPolicy("default", workspace=Workspace(ws), approve_outside=[outside_rule("git push*")])
    tool, args = boxed(), {"command": "git push origin main 2>&1", "outside_sandbox": True}
    assert policy.outside_approval(tool, args, policy.evaluate(tool, args))[0] is not None


async def test_a_tainted_session_is_refused(ws):
    """读过没信任的工作区里的文件就算污点，所以只有信任过的仓库能用。"""
    (ws / "a.txt").write_text("x", encoding="utf-8")
    h = harness(ws, [{"tool_calls": [{"name": "read", "args": {"path": "a.txt"}}]}, outside("touch ran.txt"), "好"],
                "touch ran*", trust_workspace=False)
    end = tool_ends((await h.run("跑")).events)[1]
    assert "不可信内容" in end["content"] and not (ws / "ran.txt").exists()


async def test_write_and_edit_gates_are_not_covered(ws):
    (ws / ".git" / "hooks").mkdir(parents=True)
    h = harness(ws, [{"tool_calls": [{"name": "write", "args": {"path": ".git/hooks/pre-commit", "content": "exit 0\n"}}]}, "好"],
                "touch ran*", "write *")
    end = tool_ends((await h.run("跑")).events)[0]
    assert "无人值守时一律拒绝" in end["content"] and not (ws / ".git" / "hooks" / "pre-commit").exists()


async def test_a_hook_that_wants_a_person_wins(ws):
    async def ask(_):
        return HookOutput(decision="ask", reason="守卫要人看一眼")

    h = harness(ws, [outside("touch ran.txt"), "好"], "touch ran*")
    h.hooks.add(Hook(HookEvent.PRE_TOOL_USE, ask, name="ask"))
    end = tool_ends((await h.run("跑")).events)[0]
    assert "无人值守时一律拒绝" in end["content"] and not (ws / "ran.txt").exists()


@pytest.mark.parametrize("over", [{"permissions__mode": "plan"}, {"permissions__rules": ["deny:bash(touch*)"]}])
async def test_plan_mode_and_deny_rules_still_refuse(ws, over):
    h = harness(ws, [outside("touch ran.txt"), "好"], "touch ran*", **over)
    end = tool_ends((await h.run("跑")).events)[0]
    assert "权限拒绝" in end["content"] and not (ws / "ran.txt").exists()


async def test_credential_locations_and_catastrophes_still_refuse(ws):
    h = harness(ws, [outside("cat ~/.ssh/id_rsa"), outside("rm -rf ~"), "好"], "cat *", "rm *")
    ends = tool_ends((await h.run("跑")).events)
    assert "凭证" in ends[0]["content"] and "灾难性" in ends[1]["content"]


async def test_direct_tool_calls_are_still_refused(ws):
    from flywhl_harness.cancel import CancelToken
    from flywhl_harness.runtime.workspace import Workspace
    from flywhl_harness.tools.base import ToolContext

    ctx = ToolContext(run_id="r", session_id="s", call_id="c", workspace=Workspace(ws), cancel=CancelToken(), blobs=None, idempotency_key="k")
    res = await boxed().execute({"command": "touch ran.txt", "outside_sandbox": True}, ctx)
    assert res.is_error and not (ws / "ran.txt").exists()


# ---------------------------------------------------------------- 分析器
@pytest.mark.parametrize("command", ["git push --receive-pack=x origin", "git fetch --upload-pack=x origin", "git pull --upload-pack x origin",
                                     "git clone -u x origin dir", "git clone --upload-pack=x origin dir", "git ls-remote --upload-pack=x origin",
                                     "git push ext::x main", "git fetch fd::3 main", "git clone hg::x",
                                     "git push --rece=x origin", "git push --exe=x origin", "git fetch --up=x origin",
                                     "git push --repo=ext::x main", "git clone -ux origin dir", "git clone -qu x origin dir",
                                     "git clone -c core.sshCommand=x origin dir", "git clone --conf core.sshCommand=x origin dir",
                                     "git clone --template=x origin dir", "git clone --templ x origin dir",
                                     "git pull -s x origin", "git pull -qs x origin", "git pull --strategy=x origin", "git pull --str=x origin"])
def test_git_options_that_name_a_program_are_opaque(command):
    a = analyze(command)
    assert a.opaque and a.inline and a.level != "read"


@pytest.mark.parametrize("command", ["git push -u origin main", "git push origin main", "git fetch origin", "git clone https://example.test/x.git"])
def test_plain_git_network_commands_are_not_opaque(command):
    assert not analyze(command).opaque


@pytest.mark.parametrize("command", ["git push --force origin main", "git push --force-with-lease origin main", "git push --force-w origin main",
                                     "git push -f origin main", "git push -qf origin main", "git push origin +main",
                                     "git push --mirror origin", "git push --mir origin", "git push --delete origin main",
                                     "git push --del origin main", "git push -d origin main", "git push origin :main", "git push --prune origin"])
def test_pushes_that_rewrite_the_remote_are_dangerous(command):
    assert analyze(command).level == "dangerous"


@pytest.mark.parametrize("command", ["git push --dry-run origin main", "git push --porcelain origin main", "git push --progress origin main",
                                     "git fetch --prune origin", "git push --tags origin", "git pull --stat origin", "git clone --quiet origin dir"])
def test_other_push_options_stay_ordinary(command):
    a = analyze(command)
    assert a.level == "external" and not a.opaque


@pytest.mark.parametrize("command", ["echo 'a", 'echo "a', "git status; echo \\"])
def test_commands_that_do_not_parse_are_opaque(command):
    a = analyze(command)
    assert a.opaque and a.inline and not a.segments


@pytest.mark.parametrize("command", ["rm -rf ~; echo \\", "echo a; rm -rf ~ \\", "echo a\nrm -rf ~\necho 'b", "true && mkfs.ext4 /dev/sda; echo 'b"])
def test_catastrophic_checks_still_run_when_a_command_does_not_parse(command):
    assert analyze(command).catastrophic
    assert PermissionPolicy("bypass").evaluate(boxed(), {"command": command}).decision == "deny"


def test_unparsed_commands_count_as_reading_outside_content():
    assert analyze("echo 'a").network
    assert not analyze("git status; echo \\").catastrophic


def test_allow_rules_and_auto_approval_do_not_cover_unparsed_commands():
    tool = boxed()
    tool.confinement = Confinement()
    command = {"command": "git status; touch sentinel; echo \\"}
    policy = PermissionPolicy("default", ["allow:bash(git status*)"], auto_approve=["bash(git status*)"])
    assert policy.evaluate(tool, command).decision == "ask"
    assert policy.auto_approval(tool, command, policy.evaluate(tool, command))[0] is None


def test_allow_rules_no_longer_cover_those_options():
    tool = boxed()
    tool.confinement = Confinement()
    policy = PermissionPolicy("default", ["allow:bash(git push*)"])
    assert policy.evaluate(tool, {"command": "git push origin main"}).decision == "allow"
    assert policy.evaluate(tool, {"command": "git push --receive-pack=x origin main"}).decision == "ask"


@pytest.mark.parametrize(("command", "writes"), [("echo x > a", True), ("echo x >> a", True), ("echo x &> a", True), ("echo x >| a", True),
                                                 ("echo x 2>&1", False), ("echo x > /dev/null", False), ("echo x 2>/dev/null", False),
                                                 ("cat < a", False)])
def test_writes_file_is_about_output_redirection(ws, command, writes):
    assert analyze(command, str(ws)).writes_file is writes


# ---------------------------------------------------------------- 命令行
def run_args(ws, **over) -> Namespace:
    return Namespace(**{"prompt": "x", "json": True, "thinking": None, "workspace": str(ws), "trust": True, "config": None, "provider": None,
                        "model": "mock:x", "mode": None, "yes": False, "sandbox": None, "net_allow": None, "network": None, "approve": None,
                        "approve_outside": ["git push*"], **over})


async def test_yes_and_the_flag_cannot_be_combined(ws):
    with pytest.raises(SystemExit, match="放行不了人工门"):
        await cli.cmd_run(run_args(ws, yes=True))


async def test_a_bad_pattern_stops_the_run(ws):
    with pytest.raises(SystemExit, match="--approve-outside"):
        await cli.cmd_run(run_args(ws, approve_outside=["*"]))


async def test_the_flag_is_inert_with_a_person_in_the_room(ws, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_stream_run", _finished)
    assert await cli.cmd_run(run_args(ws, json=False)) == 0
    assert "这次有人在场" in capsys.readouterr().err


async def _finished(*_args, **_kwargs) -> str:
    return "completed"
