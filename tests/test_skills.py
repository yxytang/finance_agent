"""skill 的解析与三级加载 —— 从 forge 搬过来的那部分。

全部在临时目录里跑（见 conftest 的 skills_dir / make_skill），不拿仓库里真实的
skills/ 当样本：那样改一句示例 skill 的措辞就会弄红测试。

Day 3 会往 skills/ 里放三个财务 skill，这些测试照旧 —— 它们测的是机制，
不是内容。
"""

import os

import pytest

from fa import config
from fa.prompt import build_system_prompt, render_skills_section
from fa.skills import discover, split_frontmatter
from fa.tools.skills import build_skill_tools


def use_skill():
    """取出 use_skill 工具。每次现造，免得测试之间共享状态。"""
    return build_skill_tools()[0]


# --- frontmatter 解析 ---------------------------------------------------


def test_parses_frontmatter():
    meta, body = split_frontmatter("---\nname: x\ndescription: y\n---\n正文\n")
    assert meta == {"name": "x", "description": "y"}
    assert body == "正文\n"


def test_without_frontmatter_the_whole_file_is_body():
    text = "# 标题\n\n正文\n"
    meta, body = split_frontmatter(text)
    assert meta == {}
    assert body == text


def test_dashes_must_sit_on_the_first_line():
    """`---` 出现在中间不算 frontmatter —— 整份文件都是正文。"""
    text = "\n---\nname: x\n---\n正文\n"
    meta, body = split_frontmatter(text)
    assert meta == {}
    assert body == text


def test_broken_yaml_degrades_to_no_frontmatter():
    """坏掉的 YAML 不该让这个 skill 整个消失，至少正文还得能用。"""
    text = "---\nname: [没闭合\n---\n正文\n"
    meta, body = split_frontmatter(text)
    assert meta == {}
    assert body == text


# --- 发现 ---------------------------------------------------------------


def test_name_falls_back_to_directory_name(make_skill):
    make_skill("monthly-report", "---\ndescription: 说明\n---\n正文\n")
    (skill,) = discover()
    assert skill.name == "monthly-report"


def test_description_falls_back_to_first_body_line(make_skill):
    """跳过标题行：标题说的是「它叫什么」，下一行才是「它干什么」。"""
    make_skill("x", "---\nname: x\n---\n# 标题\n\n这是正文第一句。\n")
    (skill,) = discover()
    assert skill.description == "这是正文第一句。"


def test_when_to_use_is_ignored(make_skill):
    """不认这个字段 —— 和 Claude Code 一致，触发信息只写在 description 里。

    留这条是防回归：`_describe` 一旦又开始拼额外字段，skill 共享到 Claude
    Code 那边就会少半句，而本地这边不会报任何错。
    """
    make_skill("x", "---\nname: x\ndescription: 做什么\nwhen_to_use: 何时用\n---\n")
    (skill,) = discover()
    assert skill.description == "做什么"


def test_directories_without_skill_md_are_ignored(make_skill, skills_dir):
    make_skill("good", "---\nname: good\n---\n")
    (skills_dir / "not-a-skill").mkdir()
    (skills_dir / "loose.txt").write_text("x", encoding="utf-8")

    assert [s.name for s in discover()] == ["good"]


def test_discover_follows_config_at_call_time(monkeypatch, tmp_path):
    """必须在**调用时**读 config.SKILLS_DIR。

    写成 `from fa.config import SKILLS_DIR` 就是 import 时的快照，之后再 patch
    就影响不到了。这条约定在 forge 上踩过两次，搬过来时原样保留。
    """
    directory = tmp_path / "skills"
    (directory / "only").mkdir(parents=True)
    (directory / "only" / "SKILL.md").write_text("---\nname: only\n---\n", encoding="utf-8")
    monkeypatch.setattr(config, "SKILLS_DIR", directory)

    assert [s.name for s in discover()] == ["only"]


def test_skill_order_is_stable(make_skill):
    """按目录名排序，不靠文件系统的返回顺序 —— 后者不保证稳定，
    而清单顺序一抖，指纹门控就会误判成「变了」，白费一次前缀缓存。"""
    make_skill("charlie", "---\nname: charlie\n---\n")
    make_skill("alpha", "---\nname: alpha\n---\n")
    make_skill("bravo", "---\nname: bravo\n---\n")

    assert [s.name for s in discover()] == ["alpha", "bravo", "charlie"]


# --- 第三级：资源文件 ----------------------------------------------------


def test_resources_lists_references_and_scripts(make_skill):
    directory = make_skill("x", "---\nname: x\n---\n")
    (directory / "references").mkdir()
    (directory / "references" / "a.md").write_text("A", encoding="utf-8")
    (directory / "scripts").mkdir()
    (directory / "scripts" / "run.py").write_text("print(1)", encoding="utf-8")
    (directory / "unrelated").mkdir()  # 不在这两个目录名下的不算资源

    (skill,) = discover()
    assert skill.resources() == ["references/a.md", "scripts/run.py"]


def test_read_resource_refuses_to_escape_the_skill_dir(make_skill, tmp_path):
    """第三级是唯一一条「从 skill 名字出发可能读到任意文件」的路径。

    判定必须发生在 resolve() **之后** —— 只查字符串开头的话，`../` 就能读到
    skill 目录外面去。
    """
    make_skill("x", "---\nname: x\n---\n")
    (tmp_path / "secret.txt").write_text("秘密", encoding="utf-8")

    (skill,) = discover()
    out = skill.read_resource("../secret.txt")
    assert "外面" in out
    assert "秘密" not in out


def test_read_resource_refuses_an_absolute_path(make_skill):
    """`/etc/passwd` 要单独拦一次，不能只靠 resolve 之后的边界判定。

    光靠边界判定也能拦住，但报出来的错会是「跑到目录外面去了」——
    对用户是答非所问。
    """
    make_skill("x", "---\nname: x\n---\n")
    (skill,) = discover()

    assert "绝对路径" in skill.read_resource("/etc/passwd")


@pytest.mark.skipif(os.name != "nt", reason="盘符是 Windows 特有的概念")
def test_read_resource_refuses_a_drive_relative_path(make_skill):
    """`C:foo` 这种「有盘符没根」的路径，`is_absolute()` 也是 False。

    **这条只能在 Windows 上跑。** 在 POSIX 上 `C:secret.txt` 就是个普普通通的
    文件名，`drive` 和 `root` 都是空的，没有半点特殊含义。

    留这条 `skipif` 是因为 CI 第一次跑就红了：本地 Windows 全绿，
    GitHub Actions（Ubuntu）上必挂 —— 而且挂的原因和被测代码毫无关系。
    """
    make_skill("x", "---\nname: x\n---\n")
    (skill,) = discover()

    assert "绝对路径" in skill.read_resource("C:secret.txt")


# --- use_skill 工具 ------------------------------------------------------


def test_use_skill_returns_body_and_lists_resources(make_skill):
    directory = make_skill("x", "---\nname: x\n---\n照着做\n")
    (directory / "references").mkdir()
    (directory / "references" / "a.md").write_text("A", encoding="utf-8")

    out = use_skill().invoke({"name": "x"})
    assert "照着做" in out
    assert "references/a.md" in out


def test_use_skill_reads_one_resource(make_skill):
    directory = make_skill("x", "---\nname: x\n---\n正文\n")
    (directory / "references").mkdir()
    (directory / "references" / "a.md").write_text("细则", encoding="utf-8")

    assert use_skill().invoke({"name": "x", "path": "references/a.md"}) == "细则"


def test_use_skill_unknown_name_lists_available(make_skill):
    make_skill("alpha", "---\nname: alpha\n---\n")
    out = use_skill().invoke({"name": "beta"})
    assert "没有名为" in out and "alpha" in out


def test_the_tool_description_carries_the_trigger():
    """自动触发靠的是**工具描述**，不是 system prompt。

    这条看着像在测文案，其实钉的是一个实测结论：触发条件只写在 system prompt
    里时，模型连着好几个任务都**不会**主动加载 skill；挪进工具描述之后才开始
    自动触发。因为模型在每个决策点反复读的是工具描述，system prompt 只是背景。

    改这段文案要连带重跑一次端到端，别随手删。
    """
    description = use_skill().description or ""
    assert "skill 清单" in description
    assert "先扫一遍" in description


# --- 第一级：清单里只放名字和描述 ---------------------------------------


def test_the_list_carries_only_names_and_descriptions(make_skill):
    """正文一个字都不许进 system prompt。

    这份清单在 `messages[0]` 里，**每一轮都在** —— 正文漏进来，每个 skill
    都在白吃上下文。
    """
    make_skill(
        "x",
        "---\nname: x\ndescription: 一句话说明\n---\n这段正文绝对不能出现在清单里\n",
    )

    prompt = build_system_prompt(discover())

    assert "x" in prompt and "一句话说明" in prompt
    assert "这段正文绝对不能出现在清单里" not in prompt


def test_empty_list_says_so_without_mentioning_use_skill():
    """没有 skill 时一个字都不许提 use_skill。

    提示词让模型「用某个工具去查」，而那个工具根本不在工具列表里，模型收到的
    是一条执行不了的命令，只好回一句「我这边没有」把问题挡回去。
    规则必须和事实同源。
    """
    section = render_skills_section([])
    assert "当前没有 skill" in section
    assert "use_skill" not in section


def test_over_budget_items_are_dropped_whole_not_truncated():
    """超预算时整条不列，不是把描述截半句 —— 半句话比没有更糟，
    模型会拿残缺信息去判断该不该用这个 skill。"""
    from pathlib import Path

    from fa.skills import Skill

    skills = [Skill(name=f"s{i}", description="描" * 200, path=Path("x")) for i in range(30)]
    section = render_skills_section(skills)

    assert "因篇幅未列出" in section

    listed = [line for line in section.splitlines() if line.startswith("- ")]
    assert listed, "至少得列出来一条，否则预算设小了"
    for line in listed:
        assert line.endswith("描" * 200), "列出来的必须是完整描述"


def test_same_skills_give_the_same_bytes(make_skill):
    """指纹门控全靠这条。渲染结果只要抖一下，messages[0] 就会被替换。"""
    make_skill("x", "---\nname: x\ndescription: 说明\n---\n正文\n")
    assert build_system_prompt(discover()) == build_system_prompt(discover())
