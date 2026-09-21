"""把两路检索融合成一路。

## 为什么不直接把分数加起来

两路 BM25 的分数**不可比**。标题那一路的「文档」是十几个字的 breadcrumb，正文
那一路是几百字 —— 两者的长度分布、IDF 分布完全不是一回事。

把 `0.8` 和 `12.3` 相加，等于让正文那一路单方面决定排名。融合层就白写了：
它看起来在做混合检索，实际上只是「正文检索 + 一点噪声」。

## RRF 只用排名，不用分数

    score(doc) = Σ 1 / (k + doc 在某一一路里的名次)

`k` 取 60（原论文的经验值）：第 1 名贡献 1/61，第 2 名 1/62……

为什么这样更好：**排名是两路检索唯一真正共有的东西。** 分数是各自尺度的，
「谁排在前面」不是。RRF 只依赖这个相对信息，所以天然免疫尺度问题 ——
不需要归一化，也不需要调权重。

## 已知的短板

两路都是词法的，所以**换个说法就搜不到**：用户问「怎么少交点税」，文档里写的
是「专项附加扣除」「起征点」，字面上一个都对不上。

这是纯词法检索的固有代价（DeepSeek 没有 embedding 接口），不打算藏 ——
第八天的 RAGAS `context_recall` 就是它的实测分数。到时候如果这个短板明显，
再考虑加一层同义词扩展，用数字决定要不要加，而不是先加上再说。
"""

from dataclasses import dataclass

from fa.retrieval.chunk import Chunk
from fa.retrieval.index import SearchIndex

RRF_K = 60

# 每一路先各取前多少个候选再融合。取太少会漏掉「正文排第 30 但标题排第 1」
# 这种块 —— 而它恰恰是标题那一路存在的意义。
PER_CHANNEL = 30


@dataclass(frozen=True)
class Hit:
    chunk: Chunk
    score: float
    # 被哪几路召回的。两个都命中通常比只命中一路更可信，所以把这件事
    # 带给模型看 —— 它比一个光秃秃的分数更能帮上判断。
    channels: tuple[str, ...]

    @property
    def why(self) -> str:
        return "＋".join(self.channels)


def search(
    index: SearchIndex, query: str, *, top_k: int = 5, per_channel: int = PER_CHANNEL
) -> list[Hit]:
    """两路检索 + RRF 融合。索引为空时返回空列表，不报错。"""
    if not index.chunks or not query.strip():
        return []

    fused: dict[int, float] = {}
    channels: dict[int, list[str]] = {}

    for name, label in (("title", "标题"), ("body", "正文")):
        ranked = index.rank(name, query)[:per_channel]
        for rank, (doc_id, _score) in enumerate(ranked, start=1):
            fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (RRF_K + rank)
            channels.setdefault(doc_id, []).append(label)

    hits = [
        Hit(chunk=index.chunks[doc_id], score=score, channels=tuple(channels[doc_id]))
        for doc_id, score in fused.items()
    ]
    # 同分时按「两路都命中」优先，再按原文顺序 —— 保证结果稳定，
    # 否则同一句话问两次可能拿到不同的块。
    hits.sort(key=lambda h: (-h.score, -len(h.channels), h.chunk.source))
    return hits[:top_k]


def render(hits: list[Hit]) -> str:
    """给模型看的检索结果。

    带上来源和 breadcrumb，模型才能引用；带上「命中了哪一路」，它才能判断
    这条结果有多可信。**分数不展示** —— 那是内部量纲，对模型没有意义，
    而且不同查询之间不可比，展示了只会误导。
    """
    if not hits:
        return "没检索到相关内容。"

    blocks = []
    for hit in hits:
        blocks.append(
            f"### {hit.chunk.display}   （命中：{hit.why}）\n\n{hit.chunk.text}"
        )
    return "\n\n".join(blocks)
