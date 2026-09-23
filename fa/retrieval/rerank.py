"""cross-encoder 重排。

## 为什么需要它

RRF 是**按名次相加**的：一个三路都蹭上的块，哪怕每路名次都很差，也能压过一个
只在某一路排第 1 的块。这是它的设计属性（前提是每一路都有信息量），不是 bug。

换种说法的问题上这个前提不成立 —— BM25 那两路就是噪音，而噪音 + 噪音 + 信号
大于信号。实测（第十一天）：换说法那 5 题，纯向量 4/5，等权融合只有 3/5。
**加权也修不了**（量过：2x、3x、BM25 减半都还是 3/5），因为赢家同样从向量
那一路蹭到了分。

重排不靠名次相加：cross-encoder 把 (查询, 候选) 当成**一个输入**单独打分，
所以它不受「广胜过深」的影响。而且正确答案本来就在候选集里 —— 融合的前 20
里有它，只是排在后面。

## 它不进 `search()`

重排是个**独立的函数**，`search()` 一行都不改。三个理由：

1. `eval/retrieval.py` 是「确定、免费、秒级」的闸门，而重排要一次网络往返。
2. 测试要能塞桩。塞不进桩的东西只能靠真 API 测，那就不能在 CI 上跑。
3. 它挂了必须能**退回融合顺序**，而不是让整条检索失败。分开写这件事才做得到。

## 不覆盖 `Hit.score`

`Hit.score` 还是 RRF 分。覆盖的话 `test_fusion_score_depends_only_on_ranks`
就不再是它字面上的意思了 —— 那条测试量的是「融合分只由名次决定」，而重排分
是另一个量纲的东西。
"""

import sys
from typing import Protocol

from fa.config import RagSettings
from fa.retrieval.hybrid import Hit

# 送进重排的候选数。取太少会漏掉「融合排第 15 但其实是正确答案」的块 ——
# 而那恰恰是重排存在的意义。取太多则每次都发一大坨文本，白花钱。
RERANK_CANDIDATES = 20

# 网络超时。重排在用户等答案的路径上，不能无限期挂着。
TIMEOUT = 30.0

_rerank_warned = False


class Reranker(Protocol):
    # 缓存键要用它（见 cache.py）—— 换模型必须换键，否则旧分数会被喂回来。
    model_id: str

    def scores(self, query: str, documents: list[str]) -> list[float | None]:
        """给每个文档打分，顺序和输入一一对应。判不了分的给 None。"""
        ...


class HttpReranker:
    """百炼的 /services/rerank/text-rerank。

    形状是 2026-09-23 拿真 key 实测的，不是照文档抄的：

        请求  {"model": ..., "input": {"query": ..., "documents": [...]},
               "parameters": {"return_documents": false, "top_n": N}}
        响应  {"output": {"results": [{"index": i, "relevance_score": f}, ...]}}
    """

    def __init__(self, settings: RagSettings):
        self._url = settings.rerank_base_url
        self._key = settings.api_key
        self.model_id = settings.rerank_model

    def scores(self, query: str, documents: list[str]) -> list[float | None]:
        import httpx

        response = httpx.post(
            self._url,
            headers={
                "Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model_id,
                "input": {"query": query, "documents": documents},
                "parameters": {
                    "return_documents": False,
                    "top_n": len(documents),
                },
            },
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        results = response.json()["output"]["results"]

        # 先铺一层 None，再按 index 填 —— 服务端只回它认为相关的那几条，
        # 缺席的必须留成「没分」，而不是被默认成一个数。
        out: list[float | None] = [None] * len(documents)
        for item in results:
            index = item.get("index")
            if isinstance(index, int) and 0 <= index < len(out):
                out[index] = float(item["relevance_score"])
        return out


def rerank(
    query: str,
    hits: list[Hit],
    *,
    reranker: Reranker,
    candidates: int = RERANK_CANDIDATES,
) -> list[Hit]:
    """对融合结果重排。**失败就原样返回。**

    重排是锦上添花：它挂了（网络不通、key 失效、服务端抽风）应该退回融合顺序，
    而不是让用户一个问题都问不出来。和向量那一路的取舍一样。

    `hits` 应当已经按 top_k 截断过了吗？**不** —— 调用方该先把候选集放进来
    （比最终要的多），重排之后再截。先截再排的话，被截掉的那个正好常常是
    重排该救回来的。
    """
    global _rerank_warned

    pool = hits[:candidates]
    if len(pool) <= 1 or not query.strip():
        return hits

    # 送进 cross-encoder 的是**这一段在文档里的样子**：breadcrumb + 正文。
    # 光送正文的话，像「每月 1000 元标准定额扣除」这种一个字都没提「房贷」
    # 的句子，重排也判不出它和房贷有关 —— 而 breadcrumb 里写着。
    documents = [str(hit.chunk) for hit in pool]

    try:
        scores = reranker.scores(query, documents)
    except Exception as exc:  # noqa: BLE001 - 观察者不该把被观察者弄坏
        if not _rerank_warned:
            _rerank_warned = True
            print(f"⚠️ 重排失败，这一轮用融合顺序：{exc}", file=sys.stderr)
        return hits

    if len(scores) != len(pool):
        # 服务端回的条数和发的对不上 —— 没法安全地对齐，宁可不排。
        if not _rerank_warned:
            _rerank_warned = True
            print("⚠️ 重排返回的条数和发出去的对不上，这一轮用融合顺序。", file=sys.stderr)
        return hits

    def key(pair):
        position, hit = pair
        score = scores[position]
        # 没给分的排到最后。`float("inf")` 会让它升序排第一，所以用 -inf 的
        # 思路反过来：给它一个比任何真实分数都小的值，并显式标记。
        return (
            0 if score is not None else 1,
            -(score if score is not None else 0.0),
            -hit.score,
            -len(hit.channels),
            hit.chunk.source,
            hit.chunk.breadcrumb,
        )

    ranked = [hit for _, hit in sorted(enumerate(pool), key=key)]
    return ranked + hits[candidates:]
