"""embedding / rerank 的缓存（`fa/retrieval/cache.py`）。

这个文件的重心在两件不报错的事上：

1. **键算错了**。键从内容派生是这个设计的全部意义 —— 用 id 当键的话，语料改了
   之后旧向量会被喂给新文本，检索照跑、答案悄悄是上一版的。
2. **缓存命中与不命中给出了不同的数**。那会让评测数字莫名其妙地抖，而你看不出
   原因。所以有一条测试专门断言「冷缓存和热缓存结果**逐位相同**」。

缓存**逻辑**全部由 `MemoryCache` 覆盖 —— 它不需要任何依赖，所以 CI 上跑得到。
Redis 只覆盖往返那一条（CI 装 `.[dev]`，那条会跳过）。
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from fa.retrieval.cache import (
    CACHE_SCHEME,
    MemoryCache,
    CachedEmbedder,
    CachedReranker,
    RedisCache,
    embedding_key,
    open_cache,
    pack_score,
    pack_vector,
    rerank_key,
    unpack_score,
    unpack_vector,
)

REPO = Path(__file__).resolve().parent.parent


class FakeEmbedder:
    """记账用的假 embedder —— 能看出「有几个文本真的发过去了」。"""

    model_id = "fake-embedder-v0"

    def __init__(self):
        self.batches: list[list[str]] = []

    def embed(self, texts):
        self.batches.append(list(texts))
        return [[float(len(t)), 1.0, 2.0] for t in texts]

    @property
    def call_count(self) -> int:
        return len(self.batches)


class FakeReranker:
    model_id = "fake-reranker-v0"

    def __init__(self):
        self.batches: list[tuple[str, list[str]]] = []

    def scores(self, query, documents):
        self.batches.append((query, list(documents)))
        return [float(len(d)) for d in documents]


# --- 键：从内容派生 -----------------------------------------------------


def test_the_same_text_always_gets_the_same_key():
    assert embedding_key("m", "同一段话") == embedding_key("m", "同一段话")


def test_a_different_text_gets_a_different_key():
    """**这条是整个设计的地基。**

    键从内容算，所以语料改了键就变了 —— 于是**没有「忘了清缓存」这个状态**。
    换成用位置（下标）当键的话，改了第一节之后第二节的向量会留在原地，
    然后被当成新的第二节喂回去：检索照跑，只是答的是上一版的内容。
    """
    assert embedding_key("m", "原文") != embedding_key("m", "改了一个字的原文")


def test_the_key_carries_the_model_id():
    """换 embedding 模型必须换键。

    不换的话，旧向量和新查询会被算进两个不同的向量空间，余弦值毫无意义 ——
    而症状只是「排得有点怪」。维度不同还算走运，会当场报错；维度相同就完全无声。
    """
    assert embedding_key("text-embedding-v4", "话") != embedding_key("别的模型", "话")


def test_the_key_carries_the_scheme_version(monkeypatch):
    """`CACHE_SCHEME` 是那个逃生口。

    provider 悄悄换了同名同维度的模型时，键不会变 —— 这是修不了的（不调一次
    不知道）。唯一的办法是把版本号加一、全部作废。所以它必须真的在键里，
    否则「加一」这个动作不会生效。
    """
    before = embedding_key("m", "话")

    monkeypatch.setattr("fa.retrieval.cache.CACHE_SCHEME", "v999")

    assert embedding_key("m", "话") != before


def test_the_rerank_key_is_per_document_not_per_batch():
    """**按文档算键，不按候选集算。**

    按候选集算的话，20 条候选里换进来一条新的，20 个分数全部作废 ——
    而其中 19 条一个字都没变。按文档算就只有那一条 miss。
    """
    a = rerank_key("m", "查询", "文档甲")
    b = rerank_key("m", "查询", "文档乙")

    assert a != b
    # 甲、乙各自稳定，不随「和谁一起被送去打分」而变
    assert rerank_key("m", "查询", "文档甲") == a


def test_the_rerank_key_separates_query_from_document():
    """查询和文档之间要有分隔，不能拼起来算 —— 否则「甲乙|丙」和「甲|乙丙」会撞。"""
    assert rerank_key("m", "甲乙", "丙") != rerank_key("m", "甲", "乙丙")


# --- 编解码 -------------------------------------------------------------


def test_a_vector_round_trips_exactly():
    """**逐位相同，不是「差不多」。**

    一开始用的是 float32（省一半空间），测试逼着改成 float64 —— 因为 float32
    往返之后每个分量都会差一点点，而后果不是「精度略降」：**它会让冷缓存跑一次、
    热缓存跑一次得到不同的排名**。那种差异看起来完全正常，只会让评测数字莫名
    其妙地抖。

    多花一倍空间换「缓存命中与否不影响结果」，值。
    """
    vector = [0.1234567890123456789, -1e-17, 3.0, 1 / 3]

    assert unpack_vector(pack_vector(vector)) == vector


def test_a_score_round_trips():
    assert unpack_score(pack_score(0.1717)) == 0.1717
    assert unpack_score(pack_score(None)) is None


def test_a_missing_score_is_not_zero():
    """「服务端没给分」和「给了 0 分」是两回事。

    混为一谈的话，一个没被判分的文档会顶掉一个判了、分很低但真实的文档。
    """
    assert unpack_score(pack_score(None)) is None
    assert unpack_score(pack_score(0.0)) == 0.0


# --- 包装器 -------------------------------------------------------------


def test_a_second_identical_call_does_not_reach_the_api():
    inner = FakeEmbedder()
    embedder = CachedEmbedder(inner, MemoryCache())

    embedder.embed(["甲", "乙"])
    embedder.embed(["甲", "乙"])

    assert inner.call_count == 1, "第二次还是打了一遍"


def test_only_the_missing_texts_are_sent():
    """**这条是缓存真正省钱的地方。**

    语料改了 1 个子块，只该重打那 1 个 —— 而不是 83 个全部重打。
    断言送过去的**文本内容**，不是数量：发对了数量但发错了内容一样是错的。
    """
    inner = FakeEmbedder()
    embedder = CachedEmbedder(inner, MemoryCache())
    embedder.embed(["甲", "乙", "丙"])
    inner.batches.clear()

    embedder.embed(["甲", "乙改", "丙"])

    assert inner.batches == [["乙改"]]


def test_the_result_keeps_the_caller_s_order():
    """命中、未命中混在一起时，顺序必须还是调用方给的顺序。

    错位是静默的：向量照算，只是每个块的向量其实是别人的。
    """
    inner = FakeEmbedder()
    embedder = CachedEmbedder(inner, MemoryCache())
    embedder.embed(["乙"])  # 只把乙塞进去

    out = embedder.embed(["甲", "乙", "丙"])

    assert out[1] == [float(len("乙")), 1.0, 2.0]
    assert out[0][0] == float(len("甲"))
    assert out[2][0] == float(len("丙"))


def test_a_cache_hit_gives_bit_identical_results():
    """**热缓存和冷缓存必须给出同一个数。**

    差一点点就会让评测数字抖，而那种抖动看起来像正常的「跑一次分数会动」，
    于是没人会去查。这条把「缓存随时可以删掉、删掉之后结果不变」钉住 ——
    那正是缓存该有的性质。
    """
    cold_inner = FakeEmbedder()
    cold = CachedEmbedder(cold_inner, MemoryCache()).embed(["甲", "乙", "丙"])

    warm_inner = FakeEmbedder()
    warm_cache = MemoryCache()
    CachedEmbedder(warm_inner, warm_cache).embed(["甲", "乙", "丙"])
    warm = CachedEmbedder(warm_inner, warm_cache).embed(["甲", "乙", "丙"])

    assert warm_inner.call_count == 1, "第二次全命中，不该再打一次"
    assert warm == cold


def test_the_reranker_wrapper_caches_per_document():
    inner = FakeReranker()
    reranker = CachedReranker(inner, MemoryCache())

    reranker.scores("问", ["甲", "乙"])
    reranker.scores("问", ["甲", "乙新"])

    assert inner.batches[-1] == ("问", ["乙新"])


def test_an_empty_input_does_not_call_the_api():
    inner = FakeEmbedder()
    embedder = CachedEmbedder(inner, MemoryCache())

    assert embedder.embed([]) == []
    assert inner.call_count == 0


# --- 选哪个后端 ---------------------------------------------------------


def test_no_url_means_the_in_process_cache(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)

    assert isinstance(open_cache(), MemoryCache)


def test_whitespace_only_url_means_the_in_process_cache(monkeypatch):
    """`.env` 里留一行 `REDIS_URL=` 后面跟空格，等于没填。"""
    assert isinstance(open_cache("   "), MemoryCache)


def test_an_unreachable_redis_falls_back_and_says_so(capsys):
    """**连不上是退回，不是报错。**

    缓存是加强项：它挂了应该让程序变慢，而不是让程序不能用。和向量那一路
    同一个取舍。但**要说出来** —— 静默退回会让「我配了 Redis 怎么没快」
    变成一个查不出来的问题。
    """
    cache = open_cache("redis://127.0.0.1:1/0")  # 一个没人听的端口

    assert isinstance(cache, MemoryCache)
    assert "连不上" in capsys.readouterr().out


def test_the_description_says_which_backend_it_is():
    assert "内存" in MemoryCache().describe()


# --- 懒 import ----------------------------------------------------------


def test_importing_the_cache_module_does_not_import_redis():
    """CI 装的是 `.[dev]`，没有 redis 客户端。import 期就拉进来会让 CI 直接红。"""
    code = (
        "import fa.retrieval.cache, sys;"
        "assert 'redis' not in sys.modules, 'redis 被 import 进来了';"
        "print('ok')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(REPO),
    )

    assert result.returncode == 0, result.stderr or result.stdout


# --- 真 Redis（用 fakeredis 替身，没有就跳过）--------------------------


needs_fakeredis = pytest.mark.skipif(
    importlib.util.find_spec("fakeredis") is None,
    reason="没有 fakeredis（装 .[cache]）",
)


@pytest.fixture
def fake_redis():
    fakeredis = pytest.importorskip("fakeredis")

    return fakeredis.FakeStrictRedis(decode_responses=False)


@needs_fakeredis
def test_the_redis_backend_round_trips(fake_redis):
    cache = RedisCache("redis://ignored", client=fake_redis)

    cache.set_many([("k1", b"one"), ("k2", b"two")])
    found = cache.get_many(["k1", "k2", "k3"])

    assert found == [b"one", b"two", None], "没命中的位置必须是 None"
    assert "Redis" in cache.describe()


@needs_fakeredis
def test_the_redis_backend_sets_a_ttl(fake_redis):
    """TTL 在这里管的是**内存**，不是正确性 —— 键永远不会「过期变错」。

    但它是真的存在的：不设的话 Redis 会被历史键撑满。
    """
    cache = RedisCache("redis://ignored", ttl=60, client=fake_redis)

    cache.set_many([("k", b"v")])

    ttl = fake_redis.ttl("k")
    assert 0 < ttl <= 60, f"TTL 没设上：{ttl}"


@needs_fakeredis
def test_the_redis_backend_shares_data_across_instances(fake_redis):
    """**这才是 Redis 多出来的那件事**：两个「进程」看得见同一个缓存。

    进程内的 MemoryCache 做不到这个 —— 那也正是「没 Redis 也有缓存」的原因。
    """
    one = RedisCache("redis://ignored", client=fake_redis)
    two = RedisCache("redis://ignored", client=fake_redis)

    one.set_many([("shared-key", b"value")])

    assert two.get_many(["shared-key"]) == [b"value"]


@needs_fakeredis
def test_the_end_to_end_path_works_through_redis(fake_redis):
    """走一遍真的包装器 + 真的 Redis 替身 —— 上面那些是分开测的，这条连起来。"""
    inner = FakeEmbedder()
    cache = RedisCache("redis://ignored", client=fake_redis)
    embedder = CachedEmbedder(inner, cache)

    first = embedder.embed(["甲", "乙"])
    second = CachedEmbedder(inner, cache).embed(["甲", "乙"])

    assert inner.call_count == 1
    assert second == first
