"""检索层：切块 → 建索引 → 融合检索。

分成三个模块，因为它们回答三个不同的问题：

- `chunk`  —— 什么算「一段」？按标题切，每段带上 breadcrumb
- `index`  —— 怎么算「像」？BM25，中文用字符 bigram
- `hybrid` —— 两路结果怎么合？RRF，只看排名不看分数
"""

from fa.retrieval.chunk import Chunk, chunk_markdown, load_corpus
from fa.retrieval.hybrid import Hit, render, search
from fa.retrieval.index import (
    SearchIndex,
    corpus_fingerprint,
    load,
    load_or_build,
    save,
    tokenize,
)

__all__ = [
    "Chunk",
    "Hit",
    "SearchIndex",
    "chunk_markdown",
    "corpus_fingerprint",
    "load",
    "load_corpus",
    "load_or_build",
    "render",
    "save",
    "search",
    "tokenize",
]
