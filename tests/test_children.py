"""子块切分（`fa/retrieval/children.py`）。

这里最要紧的一条是**拼回去等于原文**。切分最典型的失败不是报错，是**悄悄
吃掉几个字**：分隔符被当成分隔用掉了、没留在片段里，于是「专项附加扣除」被
切成「专项附加」+「扣除」—— 两半都在，但原文那句话没了。

那种错误没有任何症状：向量照算，检索照跑，只是命中的文字少了一点。**只有
把片段拼回去和原文比对才能发现。**
"""

import pytest

from fa.retrieval.children import CHILD_MAX_CHARS, children_of


def test_concatenating_the_children_reproduces_the_text():
    """**切分不能丢字符。**

    这条是整份文件的地基。分隔符必须留在前一段的末尾（见 `_split_after`），
    而不是被当成「分隔」消耗掉 —— 消耗掉的话，每一刀都吃掉一个字符，而
    没人会发现。

    拼不回原文 = 一定有内容被吃了，无论看起来多合理。
    """
    texts = [
        "短句。",
        "第一段。\n\n第二段。",
        "一句。两句；三句，四句。",
        "甲" * 500,  # 没有任何分隔符，只能硬切
        "一句话。" * 100,
        "开头。\n\n\n\n结尾。",
    ]

    for text in texts:
        pieces = children_of(text, max_chars=50)
        assert "".join(pieces) == text, f"拼不回去：{text[:30]!r}"


def test_this_holds_at_every_limit():
    """任何上限下，**非空白字符一个都不能丢**。

    注意这里比上一条**弱**，而这个弱是有意的：上限极小时，一段 `"\\n\\n"`
    自己就成为一个片段（比上限长），而它纯空白、会被 `children_of` 按设计丢掉。
    所以「逐字节拼回原文」在那种极端参数下**本来就不成立**。

    恒成立的、也是真正要紧的那条是：**内容不丢**。空白丢一点无所谓 —— 它不
    承载语义，而且一段纯空行的向量对检索只是噪音。

    上一条（在正常上限下逐字节相同）和这一条合起来才是完整的话：正常参数下
    一个字符都不丢，极端参数下只可能丢空白。
    """
    text = "个人所得。\n\n专项附加扣除，包括子女教育；继续教育。住房贷款利息。"

    def content(s: str) -> str:
        return "".join(s.split())

    for max_chars in (1, 5, 10, 20, 50, 1000):
        pieces = children_of(text, max_chars=max_chars)
        assert content("".join(pieces)) == content(text), f"max_chars={max_chars} 时丢了正文"


def test_no_child_exceeds_the_limit():
    """上限存在的全部意义就是不出现超长子块 —— 除非单个字符都超（那没办法）。"""
    text = "。".join(["这一段有点长" * 20] * 5)

    for max_chars in (50, 100, CHILD_MAX_CHARS):
        for piece in children_of(text, max_chars=max_chars):
            assert len(piece) <= max_chars


def test_the_coarsest_separator_wins():
    """先按空行切，而不是先按逗号 —— 段落边界比句子边界更该断。

    切在逗号上会得到两个半句，各自都不知道自己在讲什么；切在空行上得到的是
    两个完整的自然段。
    """
    text = "甲" * 80 + "。\n\n" + "乙" * 80 + "。"

    pieces = children_of(text, max_chars=100)

    assert len(pieces) == 2
    assert pieces[0].startswith("甲")
    assert pieces[1].startswith("乙")


def test_a_finer_separator_is_used_when_the_coarse_one_is_not_enough():
    """空行切完还是超长 → 降级到句号。"""
    text = "。".join(["甲" * 60] * 4)  # 没有空行，只能靠句号

    pieces = children_of(text, max_chars=100)

    assert len(pieces) > 1
    assert all(len(p) <= 100 for p in pieces)


def test_a_hard_cut_is_the_last_resort():
    """一个分隔符都没有的文本，只能硬切 —— 切在字中间不好，但比留个超长块好。"""
    text = "甲" * 250

    pieces = children_of(text, max_chars=100)

    assert [len(p) for p in pieces] == [100, 100, 50]


def test_a_short_text_is_one_child():
    """比上限短就原样返回 —— 大部分父块走的是这条路（实测 60 个里 41 个）。

    这条不是废话：它把「父子块在这个语料上近乎无操作」这个事实钉在测试里，
    免得有人读到父子块的描述之后以为它天天在起作用。
    """
    assert children_of("很短的一句话。", max_chars=200) == ["很短的一句话。"]


def test_whitespace_only_pieces_are_dropped():
    """纯空白碎片不产出子块 —— 嵌成向量只是噪音。

    代价是拼不回原文，所以丢掉的必须**只是空白**（上一条测试守着这个代价
    没有变大）。
    """
    assert children_of("   ", max_chars=50) == []
    assert children_of("\n\n\n", max_chars=50) == []
    assert children_of("", max_chars=50) == []


def test_a_blank_heavy_text_keeps_all_of_its_content():
    """空行很多时，被丢的只能是空行本身，正文一个字不能少。"""
    text = "甲。\n\n\n\n乙。\n\n丙。"

    pieces = children_of(text, max_chars=4)

    assert "".join(pieces).replace("\n", "").replace(" ", "") == "甲。乙。丙。"


def test_splitting_is_deterministic():
    """同一段文本切两次必须一样 —— 否则向量 id 和内容会对不上。"""
    text = "个人所得。\n\n专项附加扣除，包括子女教育；继续教育。"

    assert children_of(text) == children_of(text)


@pytest.mark.parametrize("text", ["甲", "甲。", "。", "甲。乙"])
def test_tiny_inputs_never_crash_or_return_nothing(text):
    """边界输入：比上限还短的东西不该被切，也不该消失。"""
    assert children_of(text, max_chars=1) != []


def test_the_child_limit_is_independent_of_the_base_chunk_limit():
    """子块上限和基础分块上限是**两个独立的**常量。

    如果这里图省事复用了 `chunk.MAX_CHUNK_CHARS`，那么有人为了调向量粒度去改
    那个值的时候，**BM25 的分块会跟着一起变** —— 索引失效、基线整体位移，
    而改的人以为自己只动了向量那一路，数字变了也找不到原因。

    （真要调子块粒度就调 `CHILD_MAX_CHARS`，它只影响向量。）
    """
    from fa.retrieval import chunk as base

    assert CHILD_MAX_CHARS != base.MAX_CHUNK_CHARS
