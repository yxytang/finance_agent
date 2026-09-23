"""embedding 和 rerank 的结果缓存。

## 为什么是这两层，不是检索结果

一次检索里贵的是**两次网络调用** —— query 的 embedding 和 rerank 的
cross-encoder。BM25 和融合是微秒级的 CPU 活，缓存它们等于缓存便宜的那一段。

更麻烦的是**检索结果是个粗键**：它得同时编码检索模式（bm25 / full / 带不带
重排）、索引指纹、两个模型 id、`top_k` 语义 —— 漏掉任何一个，表现都是**静默
返回错的命中**，正是这个项目最怕的那类错。

下面这两个是**细键**，而且都是纯函数：

    embedding(model, text)             → 向量
    rerank(model, query, document)     → 分数

## 键是内容派生的，所以这里没有「失效」

键从**内容**算出来，不是从 id。所以：

- 语料改一个字节 → 那一段的键变了 → 只有它 miss，其余 82 个子块照样命中
- **没有「忘了清缓存」这个状态** —— 这是最容易错、也最值得学的一点

反过来说，**用 id 当键是错的**：同一个 id 在语料改过之后指向的是另一段文字，
缓存会把旧向量喂给新文本。检索照跑，只是答案悄悄是上一版的。

## TTL 在这里管的是内存，不是正确性

键永远不会「过期变错」，所以 TTL 不承担正确性 —— 它只防着 Redis 被历史键撑满。
默认给一周。

## 一个我修不了的风险，写在这儿

如果 provider 悄悄换了 `text-embedding-v4` 背后的模型（同名、同维度），
键不会变，缓存会把**另一个向量空间**的向量喂回来。症状只是「排得有点怪」，
没有报错。

维度和模型名都挡不住这个。唯一的逃生口是 `CACHE_SCHEME` —— 怀疑的时候就把它
加一，全部作废重建。这和 `index.py` 的 `SCHEME`、`dense.py` 的 `CHILD_SCHEME`
是同一个套路：**方案变了就换版本号，因为内容比对不出来。**
"""

import array
import hashlib
import json
import os
from typing import Protocol, Sequence

# 键格式的版本号。改了键怎么算、或者怀疑 provider 换了模型，就加一。
# 加一等于「全部作废」，所以它是个逃生口，不是日常旋钮。
CACHE_SCHEME = "v1"

# 默认一周。理由见模块 docstring：这是内存上限，不是正确性开关。
DEFAULT_TTL_SECONDS = 7 * 24 * 3600

# 键前缀。共享一个 Redis 时，让人一眼能看出哪些键是这个项目写的。
PREFIX = "fin"


def embedding_key(model_id: str, text: str) -> str:
    """一个文本的向量键。**从内容算，不从位置算。**"""
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
    return f"{PREFIX}:emb:{CACHE_SCHEME}:{model_id}:{digest}"


def rerank_key(model_id: str, query: str, document: str) -> str:
    """一对 (查询, 文档) 的分数键。

    按**文档**而不是按候选集算键。按候选集算的话，20 条候选里换进来一条新的，
    整批 20 个分数全部作废 —— 而其中 19 个一个字都没变。
    """
    q = hashlib.sha1(query.encode("utf-8")).hexdigest()
    d = hashlib.sha1(document.encode("utf-8")).hexdigest()
    return f"{PREFIX}:rr:{CACHE_SCHEME}:{model_id}:{q}:{d}"


# --- 向量和分数的编解码 -------------------------------------------------
#
# **向量用 float64，不用 float32。** 这是纠正过来的：float32 只占一半空间
# （4KB vs 8KB，83 个子块差 300KB，无所谓），但**它不能精确往返** ——
# 存进去再读出来，每个分量都会差一点点。
#
# 后果不是「精度略降」那么轻：**它会让你冷缓存跑一次、热缓存跑一次得到略有
# 不同的排名**。而那种差异看起来完全正常，只会让评测数字莫名其妙地抖 ——
# 这个项目已经吃过一次「数字会动，而你不知道为什么」的亏。
#
# 所以宁可多花一倍空间，换「缓存命中与否不影响结果」这条性质。有测试钉着。
#
# 用标准库的 `array` 而不是 numpy：和 `dense.py` 的 MemoryStore 同一个理由，
# numpy 只是别人的传递依赖，没写在 pyproject 里。


def pack_vector(vector: Sequence[float]) -> bytes:
    return array.array("d", vector).tobytes()


def unpack_vector(raw: bytes) -> list[float]:
    values = array.array("d")
    values.frombytes(raw)
    return values.tolist()


def pack_score(score: float | None) -> bytes:
    return json.dumps(score).encode("utf-8")


def unpack_score(raw: bytes) -> float | None:
    return json.loads(raw.decode("utf-8"))


# --- 三个实现 -----------------------------------------------------------


class Cache(Protocol):
    def get_many(self, keys: Sequence[str]) -> list[bytes | None]:
        """顺序和输入一一对应，没命中的位置是 None。"""
        ...

    def set_many(self, items: Sequence[tuple[str, bytes]]) -> None: ...

    def describe(self) -> str:
        """一句话说清这是哪种后端 —— 启动时要打出来。"""
        ...


class MemoryCache:
    """进程内的 dict。

    **这不是「降级方案」，是默认方案。** 没有 `REDIS_URL` 时就用它，而它在这两层
    上已经能拿到绝大部分收益 —— 缓存的价值大部分在单进程内（同一个进程反复跑
    评测、反复问相似的问题）。

    **Redis 多出来的只是「跨进程共享」**：web 服务的几个 worker、或者评测和
    服务同时跑的时候。这个区别值得记住，不然很容易以为「没 Redis 就没缓存」。

    故意不设上限：进程退出就没了，而单进程里的键数受语料和提问量约束。
    """

    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}
        self.hits = 0
        self.misses = 0

    def get_many(self, keys: Sequence[str]) -> list[bytes | None]:
        out = []
        for key in keys:
            value = self._data.get(key)
            if value is None:
                self.misses += 1
            else:
                self.hits += 1
            out.append(value)
        return out

    def set_many(self, items: Sequence[tuple[str, bytes]]) -> None:
        for key, value in items:
            self._data[key] = value

    def describe(self) -> str:
        return f"内存缓存（进程内，{len(self._data)} 个键）"


class RedisCache:
    """真的 Redis。**懒 import** —— CI 装不上它。"""

    def __init__(self, url: str, ttl: int = DEFAULT_TTL_SECONDS, client=None):
        # `client` 只给测试用（塞一个 fakeredis 进来）。和 `build_model(cls=...)`
        # 同一个套路：**同一套配置，换一个实现**，用来在没有服务端的机器上验
        # 客户端这条路走不走得通。
        if client is None:
            import redis

            client = redis.Redis.from_url(url, decode_responses=False)
        self._client = client
        self._ttl = ttl
        self._url = url

    def get_many(self, keys: Sequence[str]) -> list[bytes | None]:
        if not keys:
            return []
        return list(self._client.mget(list(keys)))

    def set_many(self, items: Sequence[tuple[str, bytes]]) -> None:
        if not items:
            return
        # 一条 pipeline：80 多个键一条条发就是 80 多次往返。
        with self._client.pipeline() as pipe:
            for key, value in items:
                pipe.set(key, value, ex=self._ttl)
            pipe.execute()

    def describe(self) -> str:
        return f"Redis（{self._url}，TTL {self._ttl // 3600} 小时）"

    def ping(self) -> bool:
        """连得上吗。**只在启动时调一次** —— 每次检索都 ping 是白花一次往返。"""
        try:
            return bool(self._client.ping())
        except Exception:
            return False


def open_cache(url: str | None = None) -> Cache:
    """有 `REDIS_URL` 且连得上就用 Redis，否则退回进程内的。

    **连不上是退回，不是报错。** 缓存是加强项：它挂了应该让程序变慢，而不是
    让程序不能用。和向量那一路（`hybrid._semantic_ranking`）同一个取舍。

    但要**说出来** —— 静默退回会让「我配了 Redis 怎么没快」变成一个查不出来的
    问题。调用方把 `describe()` 打到启动日志里。
    """
    url = (url if url is not None else os.environ.get("REDIS_URL", "")).strip()
    if not url:
        return MemoryCache()

    try:
        cache = RedisCache(url)
    except ImportError:
        print("⚠️ 配了 REDIS_URL 但没装 redis 客户端，用进程内缓存。pip install '.[cache]'")
        return MemoryCache()

    if not cache.ping():
        print(f"⚠️ 连不上 {url}，用进程内缓存。")
        return MemoryCache()
    return cache


# --- 包装器 -------------------------------------------------------------
#
# 写成「包在原来的客户端外面」而不是把缓存塞进 `HttpEmbedder` 里面：
# 这样调用方（`build_dense` / `rerank`）一行都不用改，两边各自可测。


class CachedEmbedder:
    def __init__(self, inner, cache: Cache):
        self._inner = inner
        self._cache = cache
        self.model_id = inner.model_id

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        keys = [embedding_key(self.model_id, t) for t in texts]
        found = self._cache.get_many(keys)

        out: list[list[float] | None] = [None] * len(texts)
        missing: list[int] = []
        for i, raw in enumerate(found):
            if raw is None:
                missing.append(i)
            else:
                out[i] = unpack_vector(raw)

        if missing:
            fresh = self._inner.embed([texts[i] for i in missing])
            self._cache.set_many(
                [
                    (keys[i], pack_vector(vector))
                    for i, vector in zip(missing, fresh)
                ]
            )
            for i, vector in zip(missing, fresh):
                out[i] = vector

        return out  # type: ignore[return-value]


class CachedReranker:
    def __init__(self, inner, cache: Cache):
        self._inner = inner
        self._cache = cache
        self.model_id = getattr(inner, "model_id", "")

    def scores(self, query: str, documents: list[str]) -> list[float | None]:
        keys = [rerank_key(self.model_id, query, d) for d in documents]
        found = self._cache.get_many(keys)

        out: list[float | None] = [None] * len(documents)
        missing: list[int] = []
        for i, raw in enumerate(found):
            if raw is None:
                missing.append(i)
            else:
                out[i] = unpack_score(raw)

        if missing:
            fresh = self._inner.scores(query, [documents[i] for i in missing])
            self._cache.set_many(
                [(keys[i], pack_score(score)) for i, score in zip(missing, fresh)]
            )
            for i, score in zip(missing, fresh):
                out[i] = score

        return out
