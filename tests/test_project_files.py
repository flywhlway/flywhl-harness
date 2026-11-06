"""仓库里的文件：项目配置和自动读进上下文的文件，都不能越过信任和凭证拒绝。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from flywhl_harness.config import HarnessConfig, UntrustedConfigWarning
from flywhl_harness.prompts import find_project_context, find_skills

from .conftest import make_harness

SECRET = "SENTINEL-SECRET"


def git_init(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


def texts(workspace: Path) -> list[str]:
    return [text for _, text in find_project_context(workspace)]


@pytest.fixture
def repo(tmp_path) -> Path:
    path = tmp_path / "repo"
    git_init(path)
    return path


@pytest.fixture
def credential(tmp_path) -> Path:
    """工作区之外的凭证位置，里面只有哨兵。"""
    path = tmp_path / "home" / ".ssh" / "id_rsa"
    path.parent.mkdir(parents=True)
    path.write_text(f"{SECRET}\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------- 项目配置
@pytest.mark.parametrize("key", ["system_prompt", "append_system_prompt"])
def test_project_config_cannot_speak_for_the_harness(tmp_path, monkeypatch, key):
    (tmp_path / "flywhl.toml").write_text(f'{key} = "仓库作者的话"\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with pytest.warns(UntrustedConfigWarning, match=key):
        assert getattr(HarnessConfig.load(), key) is None


@pytest.mark.parametrize("key", ["system_prompt", "append_system_prompt"])
def test_trusted_project_config_shows_the_full_text_first(tmp_path, monkeypatch, key):
    (tmp_path / "flywhl.toml").write_text(f'{key} = "仓库作者的话"\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    shown: list[str] = []

    def trust(req) -> bool:
        shown.extend(req.risky)
        return True

    assert getattr(HarnessConfig.load(trust=trust), key) == "仓库作者的话"
    assert any("仓库作者的话" in line for line in shown), "人要看到全文才谈得上确认"


@pytest.mark.parametrize("key", ["load_agents_md", "load_skills"])
def test_project_config_cannot_turn_loading_back_on(tmp_path, monkeypatch, key):
    home = tmp_path / "fw"
    home.mkdir()
    (home / "config.toml").write_text(f"{key} = false\n", encoding="utf-8")
    monkeypatch.setenv("FLYWHL_HOME", str(home))
    (tmp_path / "flywhl.toml").write_text(f"{key} = true\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert getattr(HarnessConfig.load(), key) is False


@pytest.mark.parametrize("key", ["load_agents_md", "load_skills"])
def test_project_config_can_still_turn_loading_off(tmp_path, monkeypatch, key):
    (tmp_path / "flywhl.toml").write_text(f"{key} = false\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert getattr(HarnessConfig.load(), key) is False


# ---------------------------------------------------------------------- 符号链接
@pytest.mark.parametrize("name", ["AGENTS.md", "CLAUDE.md"])
def test_instruction_file_cannot_link_out_of_the_project(repo, credential, name):
    (repo / name).symlink_to(credential)
    with pytest.warns(UserWarning, match=name):
        assert texts(repo) == []


def test_instruction_file_cannot_link_to_a_secret_inside_the_project(repo):
    (repo / ".env").write_text(f"TOKEN={SECRET}\n", encoding="utf-8")
    (repo / "AGENTS.md").symlink_to(".env")
    with pytest.warns(UserWarning, match="AGENTS.md"):
        assert texts(repo) == []


def test_link_inside_the_project_still_loads(repo):
    (repo / "docs").mkdir()
    (repo / "docs" / "rules.md").write_text("项目规则\n", encoding="utf-8")
    (repo / "AGENTS.md").symlink_to("docs/rules.md")
    assert texts(repo) == ["项目规则\n"]


def test_skipped_link_leaves_the_other_file_in_place(repo, credential):
    (repo / "AGENTS.md").symlink_to(credential)
    (repo / "CLAUDE.md").write_text("项目规则\n", encoding="utf-8")
    with pytest.warns(UserWarning, match="AGENTS.md"):
        assert texts(repo) == ["项目规则\n"]


def test_plain_file_is_not_judged_by_where_the_project_lives(tmp_path):
    """目录名碰巧命中凭证 glob：普通文件不是链接，不用判定。"""
    project = tmp_path / "id_rsa-tools"
    git_init(project)
    (project / "AGENTS.md").write_text("项目规则\n", encoding="utf-8")
    assert texts(project) == ["项目规则\n"]


def test_skill_cannot_link_out_of_the_project(repo, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text(f"---\ndescription: {SECRET}\n---\n", encoding="utf-8")
    (repo / "skills" / "x").mkdir(parents=True)
    (repo / "skills" / "x" / "SKILL.md").symlink_to(outside)
    with pytest.warns(UserWarning, match="SKILL.md"):
        assert find_skills(repo) == []


def test_skill_inside_the_project_still_loads(repo):
    (repo / "skills" / "x").mkdir(parents=True)
    (repo / "skills" / "x" / "SKILL.md").write_text("---\ndescription: 发布流程\n---\n", encoding="utf-8")
    assert [s["description"] for s in find_skills(repo)] == ["发布流程"]


def broken_directory(path: Path) -> None:
    path.mkdir()


def broken_dangling_link(path: Path) -> None:
    path.symlink_to("missing.md")


def broken_link_loop(path: Path) -> None:
    path.symlink_to(path.name)


def broken_fifo(path: Path) -> None:
    os.mkfifo(path)


def broken_unreadable(path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root 不受文件权限限制")
    path.write_text("---\ndescription: 读不到\n---\n", encoding="utf-8")
    path.chmod(0)


BROKEN = [broken_directory, broken_dangling_link, broken_link_loop, broken_fifo, broken_unreadable]


@pytest.mark.parametrize("make", BROKEN)
def test_broken_skill_does_not_stop_the_session(repo, make):
    (repo / "skills" / "bad").mkdir(parents=True)
    (repo / "skills" / "good").mkdir()
    (repo / "skills" / "good" / "SKILL.md").write_text("---\ndescription: 发布流程\n---\n", encoding="utf-8")
    make(repo / "skills" / "bad" / "SKILL.md")
    assert [s["description"] for s in find_skills(repo)] == ["发布流程"]


@pytest.mark.parametrize("make", BROKEN)
def test_broken_instruction_file_falls_through_to_the_next_name(repo, make):
    make(repo / "AGENTS.md")
    (repo / "CLAUDE.md").write_text("项目规则\n", encoding="utf-8")
    assert texts(repo) == ["项目规则\n"]


@pytest.mark.filterwarnings("ignore::UserWarning")
async def test_linked_credential_never_reaches_the_model(repo, credential):
    (repo / "AGENTS.md").symlink_to(credential)
    h = make_harness(repo, ["好"], permissions__sandbox="off")
    await h.run("你好")
    req = h.provider.requests[0]
    assert SECRET not in (req.system or "") and all(SECRET not in (m.text or "") for m in req.messages)


# ---------------------------------------------------------------------- 项目的边界
def test_outside_a_repository_only_the_workspace_counts(tmp_path):
    (tmp_path / "AGENTS.md").write_text("上层目录的话\n", encoding="utf-8")
    workspace = tmp_path / "mine"
    workspace.mkdir()
    assert texts(workspace) == []
    (workspace / "AGENTS.md").write_text("项目规则\n", encoding="utf-8")
    assert texts(workspace) == ["项目规则\n"]


def test_inside_a_repository_ancestors_still_count(repo):
    (repo / "AGENTS.md").write_text("仓库规则\n", encoding="utf-8")
    (repo / "sub").mkdir()
    (repo / "sub" / "AGENTS.md").write_text("子目录规则\n", encoding="utf-8")
    assert texts(repo / "sub") == ["仓库规则\n", "子目录规则\n"]


def test_nested_repository_stops_at_its_own_root(repo):
    (repo / "AGENTS.md").write_text("外层规则\n", encoding="utf-8")
    nested = repo / "vendor" / "evil"
    git_init(nested)
    assert texts(nested) == []
