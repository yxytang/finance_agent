"""cross-encoder 重排（`fa/retrieval/rerank.py`）。

重排是个**网络调用**，所以这里测的重点几乎全是「它坏掉的时候会怎样」：
连不上、回得不对、分数并列。它在用户等答案的路径上，挂在这里的代价是
一个问题都问不出来。
"""

import pytest

from fa.retrieval.chunk import Chunk
from fa.retrieval.hybrid import Hit
from fa.retrieval.rerank import RERANK_CANDIDATES, rerank


def make_hits(*titles: str, channels=("标题",)) -> list[Hit]:
    return [
        Hit(
            chunk=Chunk(source="税.md", breadcrumb=f"税.md > {t}", text=f"{t}的正文。"),
            score=1.0 / (i + 1),
            channels=tuple(channels),
        )
        for i, t in enumerate(titles)
    ]


class FakeReranker:
    """按给定顺序打分的假重排 —— 分数越高排越前。"""

    def __init__(self, scores):
        self._scores = scores
        self.seen: list[list[str]] = []

    def scores(self, query, documents):
        self.seen.append(list(documents))
        return list(self._scores)


class BoomReranker:
    def scores(self, query, documents):
        raise RuntimeError("连不上")


# --- 正常重排 -----------------------------------------------------------


def test_rerank_reorders_by_the_cross_encoder_score():
    hits = make_hits("甲", "乙", "丙")
    # 丙最相关，甲最不相关
    reranker = FakeReranker([0.1, 0.2, 0.9])

    out = rerank("随便问", hits, reranker=reranker)

    assert [h.chunk.title for h in out] == ["丙", "乙", "甲"]


def test_rerank_does_not_overwrite_the_fused_score():
    """`Hit.score` 必须还是 RRF 分。

    覆盖成重排分的话，`test_fusion_score_depends_only_on_ranks`（量的是
    「融合分只由名次决定」）就不再是它字面上的意思了 —— 两个量纲混在一个
    字段里，之后谁都说不清那个数是什么。
    """
    hits = make_hits("甲", "乙")
    before = {h.chunk.breadcrumb: h.score for h in hits}

    out = rerank("随便问", hits, reranker=FakeReranker([0.9, 0.1]))

    assert [h.chunk.title for h in out] == ["甲", "乙"]  # 顺序确实变了
    assert {h.chunk.breadcrumb: h.score for h in out} == before


def test_rerank_sees_the_breadcrumb_not_just_the_body():
    """送进 cross-encoder 的必须是 breadcrumb + 正文。

    只送正文的话，像「每月 1000 元标准定额扣除」这种一个字都没提「房贷」的
    句子，重排也判不出它和房贷有关 —— 而 breadcrumb 里写着。这正是
    `chunk.py` 当年把 breadcrumb 单独存下来的原因，重排也得吃到这个好处。
    """
    reranker = FakeReranker([0.5, 0.5])
    hits = make_hits("住房贷款利息", "别的小节")

    rerank("房贷利息能扣多少", hits, reranker=reranker)

    sent = reranker.seen[0][0]
    assert "住房贷款利息" in sent, "breadcrumb 没送进去"
    assert "的正文" in sent, "正文没送进去"


# --- 坏掉的时候 ---------------------------------------------------------


def test_rerank_failure_returns_the_fused_order(monkeypatch):
    """**重排挂了不能让人问不出问题。**

    它在用户等答案的路径上。连不上就退回融合顺序 —— 那个顺序是 RRF 算出来的，
    本来就能用。抛出去的话用户一个问题都拿不到。
    """
    import fa.retrieval.rerank as module

    monkeypatch.setattr(module, "_rerank_warned", False)
    hits = make_hits("甲", "乙", "丙")

    out = rerank("随便问", hits, reranker=BoomReranker())

    assert [h.chunk.title for h in out] == ["甲", "乙", "丙"]


def test_rerank_failure_is_reported_only_once(monkeypatch, capsys):
    """只喊一次 —— 每句查询都喊一遍会把 stderr 刷满。"""
    import fa.retrieval.rerank as module

    monkeypatch.setattr(module, "_rerank_warned", False)
    hits = make_hits("甲", "乙")

    for _ in range(4):
        rerank("随便问", hits, reranker=BoomReranker())

    assert capsys.readouterr().err.count("重排失败") == 1


def test_a_length_mismatch_is_refused_rather_than_guessed(monkeypatch):
    """服务端回的条数和发出去的对不上 → 不排。

    硬对齐的话，分数会被配到错误的文档上 —— 那比不排糟得多：结果看起来是
    「重排过的」，实际是乱的。
    """
    import fa.retrieval.rerank as module

    monkeypatch.setattr(module, "_rerank_warned", False)
    hits = make_hits("甲", "乙", "丙")

    out = rerank("随便问", hits, reranker=FakeReranker([0.9, 0.1]))

    assert [h.chunk.title for h in out] == ["甲", "乙", "丙"]


def test_a_document_without_a_score_sinks_to_the_bottom():
    """服务端没给分的候选排到最后，而不是被当成 0 分塞在中间。

    0 分是个合法分数（可能真的不相关），而「没给分」是另一回事。混为一谈的话，
    一个服务端没判的文档会顶掉一个判了、分很低但真实的文档。
    """
    hits = make_hits("甲", "乙", "丙")

    out = rerank("随便问", hits, reranker=FakeReranker([None, 0.05, 0.9]))

    assert [h.chunk.title for h in out] == ["丙", "乙", "甲"]


# --- 边界 ---------------------------------------------------------------


def test_ties_keep_the_fused_order():
    """并列时保持融合顺序 —— 否则同一句话问两次可能拿到不同的块。

    cross-encoder 对相近文本给出相同分数很常见，而服务端对并列的返回顺序
    不保证稳定。所以兜底必须钉在融合分和来源上，而不是靠运气。
    """
    hits = make_hits("甲", "乙", "丙")

    out = rerank("随便问", hits, reranker=FakeReranker([0.5, 0.5, 0.5]))

    assert [h.chunk.title for h in out] == ["甲", "乙", "丙"]


def test_only_the_candidate_pool_is_reordered():
    """候选池之外的保持原样接在后面 —— 重排只负责池子里那几条。"""
    hits = make_hits(*[f"第{i}" for i in range(5)])

    out = rerank("随便问", hits, reranker=FakeReranker([0.1, 0.9]), candidates=2)

    assert [h.chunk.title for h in out[:2]] == ["第1", "第0"]
    assert [h.chunk.title for h in out[2:]] == ["第2", "第3", "第4"]


@pytest.mark.parametrize("count", [0, 1])
def test_a_tiny_candidate_pool_skips_the_call(count):
    """只有一条时没有排的必要 —— 少一次网络往返，也少一个能坏的地方。"""
    hits = make_hits(*[f"第{i}" for i in range(count)])
    reranker = FakeReranker([])

    out = rerank("随便问", hits, reranker=reranker)

    assert out == hits
    assert reranker.seen == []


def test_an_empty_query_skips_the_call():
    reranker = FakeReranker([0.5])

    rerank("   ", make_hits("甲", "乙"), reranker=reranker)

    assert reranker.seen == []


def test_the_default_candidate_pool_is_bigger_than_what_reaches_the_model():
    """候选池必须比最终给模型的多。

    先截再排的话，被截掉的那个正好常常是重排该救回来的 —— 融合「广胜过深」
    的受害者恰恰排在后面。给 4 条去重排 4 条，等于没做。
    """
    assert RERANK_CANDIDATES > 4
