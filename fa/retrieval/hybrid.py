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

两路 BM25 都是词法的，所以**换个说法就搜不到**：用户问「怎么少交点税」，文档里
写的是「专项附加扣除」「起征点」，字面上一个都对不上。

**第十一天加了第三路向量**（`"语义"`）来补这个洞。它的开关是
`SearchIndex.dense` —— 索引带了向量库它才参与，不带就还是原来那两路，行为和
加它之前逐字节一样。实测（Stage 0 探针）：换说法那 5 题 file@1 从 **1/5 到
4/5**，字面那 15 题没掉。

⚠️ **但向量路不是「白加的一路」。** RRF 里一个三路都命中的块拿 `3/(60+rank)`，
两路命中的拿 `2/(60+rank)`，所以向量能**重排**字面组 —— 而字面组原来是
14/15。下面的 `-len(h.channels)` 兜底也开始偏好三路命中。这是真实的杠杆，
`eval/retrieval.py` 的字面那一行要盯着。
"""

import sys
from dataclasses import dataclass

from fa.retrieval.chunk import Chunk
from fa.retrieval.index import SearchIndex

RRF_K = 60

# 每一路先各取前多少个候选再融合。取太少会漏掉「正文排第 30 但标题排第 1」
# 这种块 —— 而它恰恰是标题那一路存在的意义。
PER_CHANNEL = 30

# 向量那一路在模型眼里的名字。和「标题」「正文」并列，所以措辞要一致。
SEMANTIC = "语义"

# 向量失败只喊一次。每一句查询都喊一遍会把 stderr 刷满，而用户看到的信息
# 并不比第一次多。（同样的取舍见 fa/agent.py 里事件监听器抛异常那段。）
_dense_warned = False


def _semantic_ranking(index: SearchIndex, query: str, k: int) -> list[int]:
    """向量那一路的排名。**失败就返回空，绝不抛。**

    向量是加强项。它挂了 —— 网络不通、key 失效、向量库坏了 —— 正确的反应是
    退回两路 BM25，而不是让整个检索失败。用户拿不到任何结果，比拿到一个没有
    向量加持的结果糟得多。

    返回空就等于这一路不贡献任何分数（RRF 是加法的）。这也是
    `test_fusion_score_depends_only_on_ranks` 在裸索引上仍然等于 `2/(RRF_K+1)`
    的原因。
    """
    global _dense_warned
    try:
        return index.dense.search(query, k)
    except Exception as exc:  # noqa: BLE001 - 观察者不该把被观察者弄坏
        if not _dense_warned:
            _dense_warned = True
            print(f"⚠️ 向量检索失败，这一轮退回纯 BM25：{exc}", file=sys.stderr)
        return []


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
    """多路检索 + RRF 融合。索引为空时返回空列表，不报错。

    **向量那一路只在 `index.dense` 存在时才跑。** 这个函数不读环境变量、不看
    配置、不问任何全局状态 —— 它只认索引上挂了什么。理由见
    `SearchIndex.dense` 上那段：否则同一条测试会在 CI 上绿、在开发机上红。
    """
    if not index.chunks or not query.strip():
        return []

    fused: dict[int, float] = {}
    channels: dict[int, list[str]] = {}

    def rank_in(doc_ids, label: str) -> None:
        for rank, doc_id in enumerate(doc_ids, start=1):
            fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (RRF_K + rank)
            channels.setdefault(doc_id, []).append(label)

    for name, label in (("title", "标题"), ("body", "正文")):
        rank_in((doc_id for doc_id, _ in index.rank(name, query)[:per_channel]), label)

    if index.dense is not None:
        rank_in(_semantic_ranking(index, query, per_channel), SEMANTIC)

    hits = [
        Hit(chunk=index.chunks[doc_id], score=score, channels=tuple(channels[doc_id]))
        for doc_id, score in fused.items()
    ]
    # 同分时按「几路都命中」优先，再按原文顺序 —— 保证结果稳定，
    # 否则同一句话问两次可能拿到不同的块。
    #
    # 注意 `-len(h.channels)` 在加了第三路之后**也开始偏好三路命中的块**。
    # 这是真实的杠杆，不是中性的加法：见 Stage 8 要盯的「字面组有没有被拖低」。
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
