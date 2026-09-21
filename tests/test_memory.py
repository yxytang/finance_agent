"""跨会话记忆：存取 + 写入策略。

写入策略（`extract`）是这里真正有设计含量的部分：存哪里是 trivial 的，
**什么时候该记**才决定这个功能好不好用。
"""

from datetime import date

import pytest

from fa.memory import Memory, add, forget, load, parse, render, save
from fa.memory.extract import guess_kind, judge


# --- 存取 ---------------------------------------------------------------


def test_round_trip():
    memories = [
        Memory("房租归住房类", "偏好", date(2026, 9, 21)),
        Memory("target 归购物", "纠正", date(2026, 9, 22)),
    ]
    assert parse(render(memories)) == memories


def test_empty_renders_a_header_only():
    assert parse(render([])) == []


def test_hand_written_lines_are_read():
    """用户会手工编辑这个文件，所以解析要能认人写的格式。"""
    content = """
# 记忆

这是我自己的备注，不该被当成一条记忆。

- 2026-09-21 [偏好] 别把 GEICO 报成异常
- 没有日期也没有类型的一行
"""
    found = parse(content)

    assert len(found) == 1
    assert found[0].text == "别把 GEICO 报成异常"
    assert found[0].kind == "偏好"


def test_unparseable_lines_are_skipped_not_fatal():
    """多写一行注释、写错个括号，不该让整个记忆功能瘫痪。"""
    assert parse("随便写点什么\n\n还写点别的") == []


def test_missing_file_is_empty(tmp_path):
    assert load(tmp_path / "没有这个文件.md") == []


def test_add_appends_and_persists(tmp_path):
    path = tmp_path / "facts.md"
    add(path, Memory("第一条", "事实", date(2026, 1, 1)))
    add(path, Memory("第二条", "偏好", date(2026, 1, 2)))

    assert [m.text for m in load(path)] == ["第一条", "第二条"]


def test_add_is_idempotent_for_identical_text(tmp_path):
    """用户连着说两遍「记住我住北京」不该变成两条。

    重复会在注入的 prompt 里制造重复段落，而重复的内容会不成比例地放大
    自己的权重 —— 模型会以为那件事特别重要。
    """
    path = tmp_path / "facts.md"
    add(path, Memory("我住北京", "事实", date(2026, 1, 1)))
    add(path, Memory("我住北京", "事实", date(2026, 1, 2)))

    assert len(load(path)) == 1


def test_forget_by_index(tmp_path):
    path = tmp_path / "facts.md"
    save(path, [Memory(f"第{i}条") for i in range(1, 4)])

    removed = forget(path, 2)

    assert removed.text == "第2条"
    assert [m.text for m in load(path)] == ["第1条", "第3条"]


def test_forget_out_of_range_returns_none(tmp_path):
    """这个操作是给人用的，输错一个数字不该炸。"""
    path = tmp_path / "facts.md"
    save(path, [Memory("只有一条")])

    assert forget(path, 5) is None
    assert forget(path, 0) is None
    assert len(load(path)) == 1


# --- 写入策略 -----------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "这次帮我看看 3 月的账单",
        "今天花了多少",
        "刚刚那笔是不是重复扣款",
        "本次查询按类目分组",
    ],
)
def test_one_off_details_are_refused(text):
    """**一次性的任务细节最不该记。**

    「这次看看 3 月」记下来，下次用户问 4 月的时候这条记忆就是错的 ——
    而且它看起来还挺合理，所以特别难发现。一次性的东西不留痕迹反而是对的。
    """
    ok, reason = judge(text)
    assert not ok
    assert reason  # 拒绝时必须给理由，否则模型会换个说法再试一次


@pytest.mark.parametrize(
    "text",
    [
        "我的房租归住房类",
        "别把 GEICO 的车险报成异常",
        "target 应该算购物",
        "我住在上海，账单里的 SEATTLE 是以前的",
    ],
)
def test_durable_facts_are_accepted(text):
    assert judge(text)[0]


def test_too_short_is_refused():
    assert not judge("嗯")[0]


def test_the_reason_explains_what_to_do_instead():
    """理由不能只是「不行」—— 模型得知道为什么，否则它会原样再试一次。"""
    _, reason = judge("这次看看 3 月")
    assert "一次性" in reason


@pytest.mark.parametrize(
    "text,expected",
    [
        ("target 应该算购物而不是超市", "纠正"),
        ("我喜欢把咖啡单独算一类", "偏好"),
        ("我住上海", "偏好"),
        ("账单里 SEATTLE 是以前的地址", "事实"),
    ],
)
def test_guess_kind(text, expected):
    assert guess_kind(text) == expected
