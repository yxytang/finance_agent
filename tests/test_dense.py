"""向量路（`fa/retrieval/dense.py`）。

这里测的都是**不会报错的**失败：向量在库里但没人知道它对应哪个父块、换了模型
指纹没跟着换、改了正文 key 却没变。这三种都会让检索「照常运行」，只是结果不对。

全部离线：假 embedder、内存向量库。CI 上 chromadb 根本没装，而这一路必须能在
那种环境下被完整测到 —— 只有 ChromaStore 那一条 round-trip 是 skipif。
"""

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

from fa.config import RagSettings
from fa.retrieval.chunk import Chunk
from fa.retrieval.dense import (
    COLLECTION,
    ChromaStore,
    DenseIndex,
    HttpEmbedder,
    MemoryStore,
    build_dense,
    dense_fingerprint,
    load_dense,
    parent_key,
)

REPO = Path(__file__).resolve().parent.parent


def fake_vector(text: str, dim: int = 12) -> list[float]:
    """确定性向量：同样文本永远同一个方向。

    语义上没有意义（哈希不是 embedding），但**机制**能测：顺序、映射、
    复用、去重 —— 这些才是会静默出错的地方。语义质量是 Stage 8 拿真实评测
    数字说话的事，不是单元测试能替的。
    """
    digest = hashlib.sha1(text.encode("utf-8")).digest()
    return [b / 255.0 for b in digest[:dim]]


class FakeEmbedder:
    """记账用的假 embedder —— 能看出「有没有被调用」。"""

    # 指纹要用它，所以假货也得有一个。
    model_id = "fake-embedder-v0"

    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    def embed(self, texts):
        self.batches.append(list(texts))
        return [fake_vector(t) for t in texts]

    @property
    def calls(self) -> int:
        return len(self.batches)


def make_chunks(*texts: str) -> list[Chunk]:
    return [
        Chunk(source="税.md", breadcrumb=f"税.md > 第{i}节", text=text)
        for i, text in enumerate(texts)
    ]


# --- 父块标识 -----------------------------------------------------------


def test_parent_key_changes_when_only_the_text_changes():
    """**这条防的是一类看不见的错。**

    一个小节改了正文、breadcrumb 没变 —— 如果 key 只由 source + breadcrumb 算，
    key 就不变，于是**旧向量会映射到新正文上**：检索返回一段通顺、合理、但是
    另一个版本的文字。没有任何报错。

    带上正文哈希，改了内容 key 就变了，旧向量映射不上，那次命中被丢掉。
    少几条结果看得见，错文字看不见。
    """
    before = Chunk(source="税.md", breadcrumb="税.md > 起征点", text="每月 5000。")
    after = Chunk(source="税.md", breadcrumb="税.md > 起征点", text="每月 6000。")

    assert parent_key(before) != parent_key(after)


def test_parent_key_is_stable_for_the_same_chunk():
    """同样的内容必须永远同一个 key —— 否则每次重建都对不上，向量全部作废。"""
    chunk = Chunk(source="税.md", breadcrumb="税.md > 起征点", text="每月 5000。")

    assert parent_key(chunk) == parent_key(chunk)


def test_parent_key_separates_fields():
    """字段之间要有分隔，不能拼起来算 —— 否则「甲|乙」和「甲乙|」会撞。"""
    a = Chunk(source="甲", breadcrumb="乙", text="丙")
    b = Chunk(source="甲乙", breadcrumb="", text="丙")

    assert parent_key(a) != parent_key(b)


# --- 指纹 ---------------------------------------------------------------


def test_dense_fingerprint_changes_with_the_embedding_model():
    """换模型必须换指纹。

    不换的话，旧向量和新查询被算进两个不同的向量空间，余弦值毫无意义 ——
    而症状只是「排得有点怪」。维度不同会当场报错，维度相同就完全无声。
    """
    base = dense_fingerprint("corpus-abc", "text-embedding-v4")

    assert base != dense_fingerprint("corpus-abc", "text-embedding-v3")


def test_dense_fingerprint_changes_with_the_corpus():
    model = "text-embedding-v4"

    assert dense_fingerprint("corpus-a", model) != dense_fingerprint("corpus-b", model)


def test_dense_fingerprint_includes_the_child_scheme(monkeypatch):
    """改了**怎么切子块**，指纹也必须变。

    否则改了切分逻辑之后指纹纹丝不动、索引不重建，库里存的还是旧切法的向量。
    和 `index.py` 的 `SCHEME` 同一个道理，那条在 `test_retrieval.py` 里有对应的
    `test_fingerprint_includes_the_tokenize_scheme`。
    """
    before = dense_fingerprint("corpus", "model")

    monkeypatch.setattr("fa.retrieval.dense.CHILD_SCHEME", "别的切法")

    assert dense_fingerprint("corpus", "model") != before


# --- 建索引 / 复用 ------------------------------------------------------


def test_building_embeds_every_child():
    """子块数 = 嵌入的文本数，且顺序和 chunk 顺序一致。"""
    chunks = make_chunks("甲。", "乙。", "丙。")
    embedder = FakeEmbedder()
    store = MemoryStore()

    index = build_dense(chunks, embedder=embedder, store=store, fingerprint="fp")

    assert embedder.calls == 1
    assert len(embedder.batches[0]) == 3  # 三个父块各切出一个子块
    assert set(index.parents.values()) == {0, 1, 2}


def test_a_cached_index_still_maps_children_to_parents():
    """**指纹对得上、复用现成向量时，也必须把映射建出来。**

    少了这一步，向量好端端躺在库里，但没人知道它对应哪个父块 —— 于是向量路
    **每一次都静默地检索不到任何东西**。这是最容易犯的错：重建那条路走通了，
    复用那条忘了写。

    所以这里断言两件事：embedder **没有**被再调一次（确实复用了），而且映射
    是满的（复用之后还能用）。
    """
    chunks = make_chunks("甲。", "乙。")
    store = MemoryStore()
    first = FakeEmbedder()
    built = build_dense(chunks, embedder=first, store=store, fingerprint="fp")
    assert first.calls == 1
    expected = dict(built.parents)
    assert expected, "前提前提：第一次建出来得有映射，否则这条测试是空的"

    second = FakeEmbedder()
    index = load_dense(chunks, embedder=second, store=store, fingerprint="fp")

    assert second.calls == 0, "指纹没变却又打了一遍 embedding"
    assert index.parents == expected


def test_a_changed_fingerprint_rebuilds():
    chunks = make_chunks("甲。")
    store = MemoryStore()
    build_dense(chunks, embedder=FakeEmbedder(), store=store, fingerprint="old")

    embedder = FakeEmbedder()
    load_dense(chunks, embedder=embedder, store=store, fingerprint="new")

    assert embedder.calls == 1, "指纹变了却没重建"


def test_rebuilding_replaces_instead_of_accumulating():
    """换语料要**换掉**旧向量，不是往上加。

    加的话旧块还在库里、照样能被检索命中，而没有任何报错 —— 用户会看到一条
    来自上一版语料的结果。
    """
    store = MemoryStore()
    build_dense(make_chunks("甲。", "乙。"), embedder=FakeEmbedder(), store=store, fingerprint="a")

    build_dense(make_chunks("丙。"), embedder=FakeEmbedder(), store=store, fingerprint="b")

    assert store.query(fake_vector("丙。"), 10)  # 新的在
    ids = [i for i, _ in store.query(fake_vector("丙。"), 10)]
    assert len(ids) == 1, f"旧向量没被清掉：{ids}"


# --- 检索 ---------------------------------------------------------------


def test_search_returns_doc_ids_deduplicated_and_ordered():
    """一个父块的多个子块命中时，父块只能出现一次。"""
    # 一个长父块切出多个子块，另一个短父块一个
    long_text = "。".join(["这一节讲的是专项附加扣除的细节"] * 20)
    chunks = make_chunks(long_text, "很短。")
    embedder = FakeEmbedder()
    store = MemoryStore()
    index = build_dense(chunks, embedder=embedder, store=store, fingerprint="fp")
    assert len(index.parents) > 2, "前提前提：长父块确实切出了多个子块"

    hits = index.search(long_text, 10)

    assert len(hits) == len(set(hits)), "同一个父块出现了多次"


def test_orphan_children_are_dropped_not_mismapped():
    """库里有个没人认识的 id → **丢掉那次命中**，不是猜一个父块。

    猜的话会返回一段和查询无关的正文，而且看起来完全正常。丢掉只是少一条结果。
    """
    store = MemoryStore()
    store.reset("fp", ["孤儿#0"], [fake_vector("孤儿#0")])
    index = DenseIndex(embedder=FakeEmbedder(), store=store, parents={})

    assert index.search("随便问", 5) == []


def test_search_on_an_empty_index_is_not_an_error():
    index = DenseIndex(embedder=FakeEmbedder(), store=MemoryStore(), parents={})

    assert index.search("随便问", 5) == []
    assert index.search("", 5) == []


# --- 内存向量库 ---------------------------------------------------------


def test_memory_store_orders_by_distance():
    store = MemoryStore()
    store.reset("fp", ["近", "远"], [fake_vector("甲"), fake_vector("乙")])

    hits = store.query(fake_vector("甲"), 2)

    assert hits[0][0] == "近"
    assert hits[0][1] < hits[1][1]


def test_memory_store_breaks_ties_deterministically():
    """两个向量一模一样时必须给出**稳定**的顺序。

    靠插入顺序兜底不行：库里顺序变了，同一查询两次的排名就不一样，而
    `eval/retrieval.py` 的数字会跟着抖。
    """
    vector = fake_vector("甲")
    store = MemoryStore()
    store.reset("fp", ["b", "a"], [vector, vector])

    assert [i for i, _ in store.query(vector, 2)] == ["a", "b"]


# --- embedding 客户端 ---------------------------------------------------


def test_the_http_embedder_batches_at_ten():
    """`text-embedding-v4` 单次最多 10 条 —— 这是实测出来的，不是文档抄的。

    超了服务端直接 400，而不是自动分批。所以分批必须在我们这边。
    """
    embedder = HttpEmbedder(
        RagSettings(
            api_key="x",
            embed_base_url="https://example.com/v1",
            embed_model="text-embedding-v4",
            rerank_base_url="",
            rerank_model="",
        )
    )
    sizes: list[int] = []

    class FakeClient:
        class embeddings:  # noqa: N801
            @staticmethod
            def create(model, input):
                sizes.append(len(input))
                data = [
                    type("D", (), {"index": i, "embedding": [float(i)]})()
                    for i in range(len(input))
                ]
                return type("R", (), {"data": data})()

    embedder._client = FakeClient()

    out = embedder.embed([f"第{i}条" for i in range(25)])

    assert sizes == [10, 10, 5]
    assert len(out) == 25


def test_the_http_embedder_restores_the_server_order():
    """服务端不保证保序 —— 按 index 排回来，否则向量和文本会错位。

    错位是静默的：向量照算，检索照跑，只是每个块的向量其实是别人的。
    """
    embedder = HttpEmbedder(
        RagSettings("x", "https://example.com/v1", "m", "", "")
    )

    class FakeClient:
        class embeddings:  # noqa: N801
            @staticmethod
            def create(model, input):
                # 故意**倒序**返回
                data = [
                    type("D", (), {"index": i, "embedding": [float(i)]})()
                    for i in reversed(range(len(input)))
                ]
                return type("R", (), {"data": data})()

    embedder._client = FakeClient()

    assert embedder.embed(["a", "b", "c"]) == [[0.0], [1.0], [2.0]]


# --- 懒 import ----------------------------------------------------------


def test_importing_the_retrieval_package_does_not_import_chromadb():
    """**CI 装不上 chromadb，所以它必须是懒的。**

    在 import 期就把 chromadb 拉进来的话，CI 上任何一条碰到 `fa.retrieval` 的
    测试都会 ImportError —— 而 CI 恰恰只装 `.[dev]`。

    （和 `tests/test_eval.py` 里那条 ragas 的懒 import 不变式同一个道理：
    第九天就是被这个咬过 —— ragas 顺着 import 链进了测试，200 多兆。）

    这里连 **agent 那条路**一起查（`fa.tools.knowledge` 和 `eval.retrieval`）——
    它们是 CI 真的会 import 到的东西，只查 `fa.retrieval` 不够。
    """
    code = (
        "import fa.retrieval, fa.retrieval.dense, fa.tools.knowledge, eval.retrieval, sys;"
        "assert 'chromadb' not in sys.modules, 'chromadb 被 import 进来了';"
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


# --- ChromaStore（装不上就跳过）-----------------------------------------


@pytest.mark.skipif(
    __import__("importlib.util", fromlist=["util"]).find_spec("chromadb") is None,
    reason="没有 chromadb",
)
def test_chroma_store_round_trips(tmp_path):
    store = ChromaStore(tmp_path / "chroma")
    store.reset("fp-1", ["a", "b"], [fake_vector("甲"), fake_vector("乙")])

    assert store.stamp() == "fp-1"
    hits = store.query(fake_vector("甲"), 2)
    assert hits[0][0] == "a"


@pytest.mark.skipif(
    __import__("importlib.util", fromlist=["util"]).find_spec("chromadb") is None,
    reason="没有 chromadb",
)
def test_chroma_store_resets_rather_than_accumulating(tmp_path):
    """换指纹要**删了重建**，不是 upsert。

    upsert 会让两份语料的向量共存 —— 旧块还在库里、照样能被命中，而且没有
    任何报错。
    """
    store = ChromaStore(tmp_path / "chroma")
    store.reset("fp-1", ["a", "b"], [fake_vector("甲"), fake_vector("乙")])
    store.reset("fp-2", ["c"], [fake_vector("丙")])

    ids = [i for i, _ in store.query(fake_vector("丙"), 10)]

    assert ids == ["c"], f"旧向量没清掉：{ids}"
    assert store.stamp() == "fp-2"


@pytest.mark.skipif(
    __import__("importlib.util", fromlist=["util"]).find_spec("chromadb") is None,
    reason="没有 chromadb",
)
def test_chroma_store_survives_a_reopen(tmp_path):
    """落盘了才算真的存住了 —— 否则每次启动都要重打一遍 embedding（要钱）。"""
    ChromaStore(tmp_path / "chroma").reset("fp", ["a"], [fake_vector("甲")])

    reopened = ChromaStore(tmp_path / "chroma")

    assert reopened.stamp() == "fp"
    assert reopened.query(fake_vector("甲"), 1)[0][0] == "a"


def test_an_empty_store_has_no_stamp():
    """没建过库时 stamp 是空的 —— 调用方靠这个决定要不要重建。"""
    assert MemoryStore().stamp() == ""


def test_the_collection_name_meets_chromas_rule():
    """chromadb 要求名字 3-512 字符、只含 [a-zA-Z0-9._-]，且首尾是字母数字。

    这是个**装了跑一遍才知道**的约束（第一次拿 "t" 试就被拒了）。写死在这里，
    免得有人觉得名字太长想改短。
    """
    assert 3 <= len(COLLECTION) <= 512
    assert COLLECTION == COLLECTION.strip()
    assert all(c.isalnum() or c in "._-" for c in COLLECTION)
    assert COLLECTION[0].isalnum() and COLLECTION[-1].isalnum()


# --- 作为第三路接进融合 -------------------------------------------------
#
# 这一组守的是同一个决定：**向量参不参与，是索引的属性，不是环境的属性。**
# 如果 `hybrid.search` 改成读环境变量或模块级单例来判定，下面的第一、二条会
# 在 CI 上绿、在配了 key 的开发机上红 —— 环境相关的静默失败。


def make_index(texts: list[str], *, dense=None):
    from fa.retrieval.index import SearchIndex

    index = SearchIndex().build(
        [Chunk("税.md", f"税.md > 第{i}节", t) for i, t in enumerate(texts)],
        fingerprint="fp",
    )
    index.dense = dense
    return index


class ExplodingDense:
    """一个一调就炸的向量路 —— 用来验降级。"""

    def __init__(self) -> None:
        self.attempts = 0

    def search(self, query, k):
        self.attempts += 1
        raise RuntimeError("向量库坏了")


def test_the_semantic_channel_only_appears_when_the_index_carries_one():
    """索引上没挂向量库 → 只有标题和正文两路。

    这条是**环境泄漏的探针**。它必须只依赖传进来的索引，不依赖这台机器上
    有没有配 key —— 否则同一条断言在 CI 和开发机上结论不同。
    """
    from fa.retrieval.hybrid import search

    hits = search(make_index(["目标词。", "别的。"]), "目标词", top_k=2)

    assert hits
    assert all("语义" not in hit.channels for hit in hits)


def test_the_semantic_channel_shows_up_when_the_index_carries_one():
    """挂了向量库、而且它召回了东西 → 标签里要有「语义」。

    模型靠这个标签判断一条结果有多可信（`render` 会把它写给模型看），
    所以它得真的出现。
    """
    from fa.retrieval.hybrid import search

    store = MemoryStore()
    embedder = FakeEmbedder()
    chunks = make_chunks("目标词在这一节里。", "别的。")
    dense = build_dense(chunks, embedder=embedder, store=store, fingerprint="fp")

    index = make_index(["目标词在这一节里。", "别的。"], dense=dense)
    hits = search(index, "目标词在这一节里。", top_k=2)

    assert hits
    assert "语义" in hits[0].channels


def test_a_dense_failure_degrades_to_bm25_rather_than_raising(monkeypatch, capsys):
    """**向量挂了不能让检索整个失败。**

    网络不通、key 失效、向量库坏了 —— 这些都是向量路自己的事。用户拿不到任何
    结果，比拿到一个没有向量加持的结果糟得多。

    所以它必须退化成两路 BM25，而不是抛出去。同时**要喊一声** —— 静默降级
    等于让用户以为向量一直在起作用。
    """
    import fa.retrieval.hybrid as hybrid

    monkeypatch.setattr(hybrid, "_dense_warned", False)
    dense = ExplodingDense()
    index = make_index(["目标词。", "别的。"], dense=dense)

    hits = hybrid.search(index, "目标词", top_k=2)

    assert dense.attempts == 1, "向量路根本没被调到，这条测试是空的"
    assert hits, "向量挂了就把 BM25 的结果也弄没了"
    assert all("语义" not in hit.channels for hit in hits)
    assert "向量检索失败" in capsys.readouterr().err


def test_a_dense_failure_is_reported_only_once(monkeypatch, capsys):
    """只喊一次。每句查询都喊会把 stderr 刷满，而信息并不比第一次多。"""
    import fa.retrieval.hybrid as hybrid

    monkeypatch.setattr(hybrid, "_dense_warned", False)
    index = make_index(["目标词。", "别的。"], dense=ExplodingDense())

    for _ in range(5):
        hybrid.search(index, "目标词", top_k=2)

    assert capsys.readouterr().err.count("向量检索失败") == 1


def test_a_silent_dense_channel_leaves_the_ranking_untouched():
    """向量一路没召回任何东西时，排名必须和没有它**完全一样**。

    RRF 是加法的，所以「召回空」就等于「贡献 0」。这条把这件事钉住 ——
    如果哪天有人给它加个基础分或者占位，排名会悄悄变，而 `channels` 上看不出来。
    """
    from fa.retrieval.hybrid import search

    class EmptyDense:
        def search(self, query, k):
            return []

    texts = ["目标词。", "目标词也在。", "别的。"]

    without = [h.chunk.breadcrumb for h in search(make_index(texts), "目标词", top_k=3)]
    with_it = [
        h.chunk.breadcrumb
        for h in search(make_index(texts, dense=EmptyDense()), "目标词", top_k=3)
    ]

    assert without == with_it


def test_load_or_build_attaches_the_dense_index_even_when_the_cache_hits(tmp_path):
    """**复用的那条路也要挂上向量库。**

    这是整个 Stage 3 最容易犯的错：`load()` 是显式构造 `SearchIndex(chunks=...,
    fingerprint=...)` 的，不认识 `dense` 这个字段。只在重建那条分支挂的话，
    第一次检索向量好使，**从第二次起静默退回 BM25** —— 没有报错，只是结果
    变差了。

    所以这里走两遍 `load_or_build`：第一遍重建并落盘，第二遍命中缓存。
    第二遍必须仍然带着向量路。
    """
    from fa.retrieval.index import load_or_build

    (tmp_path / "税.md").write_text(
        "# 税\n\n目标词在这一节里，这句正文必须超过三十个字符，"
        "否则会被 chunk_markdown 当成只有标题的占位块丢掉。\n",
        encoding="utf-8",
    )
    store = MemoryStore()
    embedder = FakeEmbedder()

    first = load_or_build(
        tmp_path, tmp_path / ".index.json", embedder=embedder, store=store
    )
    assert first.dense is not None

    second = load_or_build(
        tmp_path, tmp_path / ".index.json", embedder=embedder, store=store
    )

    assert second.dense is not None, "命中缓存之后向量路消失了"
    assert second.dense.parents, "复用路径上没建映射，等于检索不到任何东西"


def test_load_or_build_without_an_embedder_behaves_exactly_as_before(tmp_path):
    """不传 embedder 时**一个字节都不能变**。

    CI 和绝大多数测试走的是这条路。它要是挂了或行为变了，整条既有基线就没了。
    """
    from fa.retrieval.index import load_or_build

    (tmp_path / "税.md").write_text(
        "# 税\n\n目标词在这一节里，这句正文必须超过三十个字符，"
        "否则会被 chunk_markdown 当成只有标题的占位块丢掉。\n",
        encoding="utf-8",
    )

    index = load_or_build(tmp_path, tmp_path / ".index.json")

    assert index.dense is None
    assert index.chunks
