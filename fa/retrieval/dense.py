"""向量路 —— embedding 客户端 + 向量库。

## Chroma 是「派生缓存」，不是第二个真相

父块**永远只住在 `index.json` 里**。这里只存向量，id 由父块内容算出来。

理由不是洁癖。两个 store 各存一份父块的话，它们会不同步，而**不同步的表现
是「检索返回了另一个版本的正文」** —— 文字通顺、看着合理、没有报错。让父块
只有一个家，这类失败就不存在。

## 指纹

    dense_fp = sha1(CHILD_SCHEME ‖ embed_model ‖ corpus_fingerprint)

换 embedding 模型**必须**换指纹。不换的话，旧向量和新查询会被算进两个不同的
向量空间，余弦值毫无意义 —— 而症状只是「排得有点怪」。维度不同还算走运，
会当场报错；维度相同就完全无声。

## batch 上限

`text-embedding-v4` 单次最多 10 条输入，超了直接 400。这是实测出来的
（`Value error, batch size is invalid, it should not be larger than 10`），
不是从文档抄的。
"""

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

from fa.config import RagSettings
from fa.retrieval.chunk import Chunk
from fa.retrieval.children import children_of

# 切分方案 + 向量方案的版本号。**改了子块怎么切、或者换了向量模型，都要改它**，
# 否则指纹察觉不到，索引会静默地按旧方案服务。同一个道理见 index.py 的 SCHEME。
CHILD_SCHEME = "recursive-sep-v1"

# text-embedding-v4 的硬限制。别的模型可能不一样，所以这是个可以调的常数。
EMBED_BATCH = 10

# Chroma 的 collection 名字：至少 3 个字符，只能用 [a-zA-Z0-9._-]。
COLLECTION = "knowledge"


# --- embedding ----------------------------------------------------------


class Embedder(Protocol):
    # 指纹要用它。换模型必须换指纹，理由见模块 docstring。
    model_id: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """把一批文本变成向量，顺序和输入一一对应。"""
        ...


class HttpEmbedder:
    """走 OpenAI 兼容的 /embeddings。"""

    def __init__(self, settings: RagSettings):
        # 延迟到构造时才 import —— openai 是 langchain-openai 带进来的，
        # 但让这个模块在 import 期就依赖它没必要。
        from openai import OpenAI

        self._client = OpenAI(
            api_key=settings.api_key, base_url=settings.embed_base_url
        )
        self.model_id = settings.embed_model

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for start in range(0, len(texts), EMBED_BATCH):
            batch = list(texts[start : start + EMBED_BATCH])
            resp = self._client.embeddings.create(model=self.model_id, input=batch)
            # 按 index 排回来，不假定服务端保序
            out.extend(d.embedding for d in sorted(resp.data, key=lambda d: d.index))
        return out


# --- 向量库 -------------------------------------------------------------


class VectorStore(Protocol):
    def stamp(self) -> str:
        """库里现在的指纹。空的表示没有库。"""
        ...

    def reset(self, stamp: str, ids: list[str], vectors: list[list[float]]) -> None:
        """**整条重建**，不是往旧的上面加。"""
        ...

    def query(self, vector: list[float], k: int) -> list[tuple[str, float]]:
        """按距离升序返回 [(id, 距离)]。"""
        ...


def _cosine_distance(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if not na or not nb:
        return 1.0
    return 1.0 - dot / (na * nb)


class MemoryStore:
    """暴力余弦，纯标准库。

    **这是测试用的**，不是降级路径。它能存在，意味着向量那一路的逻辑可以在
    `chromadb` 一个字节都没装的情况下被完整测到 —— 而 CI 装的正是 `.[dev]`，
    没有 chromadb。

    刻意不用 numpy：numpy 只是别的包的传递依赖，没写在 `pyproject.toml` 里。
    靠它等于让「离线可测」这个保证变成偶然。83 个向量手算余弦是免费的。
    """

    def __init__(self) -> None:
        self._stamp = ""
        self._ids: list[str] = []
        self._vectors: list[list[float]] = []

    def stamp(self) -> str:
        return self._stamp

    def reset(self, stamp: str, ids: list[str], vectors: list[list[float]]) -> None:
        self._stamp = stamp
        self._ids = list(ids)
        self._vectors = [list(v) for v in vectors]

    def query(self, vector: list[float], k: int) -> list[tuple[str, float]]:
        scored = [
            (_cosine_distance(vector, v), i) for i, v in enumerate(self._vectors)
        ]
        # 并列时按 id 兜底 —— 同分不同序会让同一查询两次给出不同的排名。
        scored.sort(key=lambda kv: (kv[0], self._ids[kv[1]]))
        return [(self._ids[i], d) for d, i in scored[:k]]


class ChromaStore:
    """chromadb。**懒 import** —— CI 装不上它，这个模块必须在那种环境下也能被
    import（`MemoryStore` 走同一套接口）。"""

    def __init__(self, directory: Path):
        import chromadb

        self._client = chromadb.PersistentClient(path=str(directory))

    def stamp(self) -> str:
        try:
            collection = self._client.get_collection(COLLECTION)
        except Exception:
            return ""
        return str((collection.metadata or {}).get("stamp", ""))

    def reset(self, stamp: str, ids: list[str], vectors: list[list[float]]) -> None:
        # **先删再建，不是 upsert。** upsert 会让两份语料的向量共存 —— 换了
        # 语料之后旧块还在库里，检索照样命中它们，而且没有任何报错。
        try:
            self._client.delete_collection(COLLECTION)
        except Exception:
            pass  # 本来就没有，正常

        collection = self._client.create_collection(
            COLLECTION,
            metadata={"stamp": stamp, "hnsw:space": "cosine"},
        )
        if ids:
            collection.upsert(ids=ids, embeddings=[list(v) for v in vectors])

    def query(self, vector: list[float], k: int) -> list[tuple[str, float]]:
        try:
            collection = self._client.get_collection(COLLECTION)
        except Exception:
            return []
        if collection.count() == 0:
            return []
        result = collection.query(query_embeddings=[list(vector)], n_results=k)
        ids = result.get("ids") or [[]]
        distances = result.get("distances") or [[]]
        return list(zip(ids[0], distances[0]))


def open_store(directory: Path, *, prefer_chroma: bool = True):
    """开一个向量库。chromadb 装不上就退回内存版。

    退回内存版意味着**每次都要重新打 embedding**（内存库不持久）。所以这不是
    静默降级 —— 调用方要把「这次用的是哪个」报到界面上，见 `dense_status()`。
    """
    if not prefer_chroma:
        return MemoryStore()
    try:
        return ChromaStore(directory)
    except Exception:
        return MemoryStore()


# --- 索引 ---------------------------------------------------------------


def parent_key(chunk: Chunk) -> str:
    """父块的稳定标识。**带上正文哈希。**

    不带正文的话，一个小节改了内容但 breadcrumb 没变，旧向量就会映射到**新的**
    正文上 —— 检索返回一段看起来合理、其实是另一个版本的文字，全程没有报错。

    带上哈希，改了内容 key 就变了，旧向量映射不上，那次命中被丢掉。**宁可少
    几条结果，也不要返回错的文字**：少几条看得见，错文字看不见。
    """
    digest = hashlib.sha1()
    for part in (chunk.source, chunk.breadcrumb, chunk.text):
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def dense_fingerprint(corpus_fp: str, embed_model: str) -> str:
    digest = hashlib.sha1()
    for part in (CHILD_SCHEME, embed_model, corpus_fp):
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


@dataclass
class DenseIndex:
    """向量路的检索入口。

    `parents` 是 child_id → 父块在 `index.json` 里的 doc_id。**查不到就丢掉
    那次命中**，不是猜一个。
    """

    embedder: Embedder
    store: object
    parents: dict[str, int]

    def search(self, query: str, k: int) -> list[int]:
        """返回按相关度排好的父块 doc_id（去重、保序）。"""
        if not self.parents or not query.strip():
            return []

        vectors = self.embedder.embed([query])
        if not vectors:
            return []

        out: list[int] = []
        seen: set[int] = set()
        for child_id, _distance in self.store.query(vectors[0], k):
            parent = self.parents.get(child_id)
            if parent is None or parent in seen:
                continue
            seen.add(parent)
            out.append(parent)
        return out


def build_dense(
    chunks: list[Chunk], *, embedder: Embedder, store, fingerprint: str
) -> DenseIndex:
    """把父块切成的子块全部嵌入，写进向量库。"""
    ids: list[str] = []
    texts: list[str] = []
    parents: dict[str, int] = {}

    for doc_id, chunk in enumerate(chunks):
        key = parent_key(chunk)
        for index, child in enumerate(children_of(chunk.text)):
            child_id = f"{key}#{index}"
            ids.append(child_id)
            texts.append(child)
            parents[child_id] = doc_id

    vectors = embedder.embed(texts) if texts else []
    store.reset(fingerprint, ids, vectors)
    return DenseIndex(embedder=embedder, store=store, parents=parents)


def load_dense(
    chunks: list[Chunk], *, embedder: Embedder, store, fingerprint: str
) -> DenseIndex:
    """指纹对得上就复用库里现成的向量，否则重建。

    **复用的时候也要把 `parents` 建出来。** 少了这一步，向量在库里、但没人
    知道它对应哪个父块，于是向量路**静默地什么都检索不到** —— 每一次。这是
    最容易犯的一个错：重建那条路走通了，复用那条忘了。
    """
    if store.stamp() == fingerprint:
        parents: dict[str, int] = {}
        for doc_id, chunk in enumerate(chunks):
            key = parent_key(chunk)
            for index, _ in enumerate(children_of(chunk.text)):
                parents[f"{key}#{index}"] = doc_id
        return DenseIndex(embedder=embedder, store=store, parents=parents)

    return build_dense(
        chunks, embedder=embedder, store=store, fingerprint=fingerprint
    )
