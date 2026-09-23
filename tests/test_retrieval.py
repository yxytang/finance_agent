"""检索：切块 / 分词 / BM25 / 融合。

这个文件里最值得看的是 **breadcrumb 那两条**和**停用词那一条** ——
它们各自对应一个实测出来的失败，不是理论上「应该这样」。
"""

from pathlib import Path

import pytest

from fa.retrieval import chunk_markdown, load_corpus, search
from fa.retrieval.chunk import Chunk, MAX_CHUNK_CHARS
from fa.retrieval.hybrid import RRF_K
from fa.retrieval.index import (
    SCHEME,
    SearchIndex,
    corpus_fingerprint,
    load,
    load_or_build,
    save,
    tokenize,
)


def write(root: Path, name: str, text: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# --- 切块 ---------------------------------------------------------------

# 正文必须写得**比 MIN_CHUNK_CHARS 长**，否则会被当成「只有标题没有内容」的
# 占位块丢掉。第一版测试里的「足够长」其实只有二十来个字，一条断言红了半天
# 才想起来是这个阈值 —— 所以这里用一个显式够长的常量。
LONG = "这是一段足够长的正文，长到能稳稳地通过最小长度的检查。" * 2


def test_splits_on_headings():
    chunks = chunk_markdown(
        f"# 标题\n\n{LONG}\n\n## 第一节\n\n{LONG}\n\n## 第二节\n\n{LONG}\n",
        "测试.md",
    )
    assert len(chunks) == 3
    assert chunks[1].title == "第一节"
    assert chunks[2].title == "第二节"


def test_breadcrumb_carries_the_whole_heading_path():
    """**这条是切块存在的理由。**

    用户问「房贷利息能抵多少」，正文里可能一个字都不提「房贷」也不提「个税」。
    救它的是 breadcrumb 里的标题层级。
    """
    chunks = chunk_markdown(
        "# 个税基础\n\n## 专项附加扣除\n\n### 住房贷款利息\n\n"
        "首套住房贷款利息按每月 1000 元标准定额扣除，扣除期限最长 240 个月。\n",
        "个税基础.md",
    )
    assert chunks[-1].breadcrumb == "个税基础.md > 个税基础 > 专项附加扣除 > 住房贷款利息"


def test_short_sections_are_dropped():
    """只有标题没有内容的小节进去只会添噪音。"""
    chunks = chunk_markdown(f"# 标题\n\n## 空小节\n\n## 有内容的\n\n{LONG}\n", "x.md")
    assert [c.title for c in chunks] == ["有内容的"]


def test_a_single_oversized_paragraph_is_hard_split():
    """段落本身超过上限时要硬切。

    不硬切的话这个块会以超限的状态留在索引里，而分块上限存在的全部意义就是
    不出现这种块。硬切会切断句子，但被切断的块仍能被检索到。
    """
    huge = "很长的一段话，中间没有任何空行。" * 200
    chunks = chunk_markdown(f"# 标题\n\n{huge}\n", "x.md")

    assert len(chunks) > 1
    assert all(len(c.text) <= MAX_CHUNK_CHARS for c in chunks)


def test_long_sections_are_split_on_paragraphs():
    paragraph = "这是一段够长但没超上限的正文。" * 20
    chunks = chunk_markdown(f"# 标题\n\n" + f"{paragraph}\n\n" * 8, "x.md")

    assert len(chunks) > 1
    assert all(len(c.text) <= MAX_CHUNK_CHARS for c in chunks)
    # 续块的 breadcrumb 要标出来，否则两个块看起来是同一个地方的两份副本
    assert "（续）" in chunks[1].breadcrumb


def test_document_without_headings_still_chunks():
    """总比什么都检索不到强，而且它会拿到文件名当 breadcrumb。"""
    chunks = chunk_markdown(f"{LONG}\n", "x.md")
    assert len(chunks) == 1
    assert chunks[0].breadcrumb == "x.md"


def test_load_corpus_reads_markdown(tmp_path):
    write(tmp_path, "a.md", f"# A\n\n{LONG}\n")
    write(tmp_path, "sub/b.md", f"# B\n\n{LONG}\n")
    write(tmp_path, "忽略.txt", "不是 markdown")

    sources = {c.source for c in load_corpus(tmp_path)}
    assert sources == {"a.md", "sub/b.md"}
    write(tmp_path, "sub/b.md", f"# B\n\n{LONG}\n")
    write(tmp_path, "忽略.txt", "不是 markdown")

    sources = {c.source for c in load_corpus(tmp_path)}
    assert sources == {"a.md", "sub/b.md"}


# --- 分词 ---------------------------------------------------------------


def test_cjk_gets_bigrams_and_unigrams():
    tokens = tokenize("税费", drop_stopwords=False)
    assert "税费" in tokens
    assert "税" in tokens and "费" in tokens


def test_bigrams_are_what_separates_similar_words():
    """单字分不出「税费」和「费用」，bigram 分得出 —— 这是用 bigram 的全部理由。"""
    a = set(tokenize("税费", drop_stopwords=False))
    b = set(tokenize("费用", drop_stopwords=False))
    assert "税费" not in b
    assert "费用" not in a
    assert a & b == {"费"}  # 只共用一个字


def test_ascii_runs_stay_whole():
    assert "lpr" in tokenize("LPR 利率", drop_stopwords=False)


def test_stopwords_are_dropped_by_default():
    assert "什么" not in tokenize("LPR 是什么")
    assert "lpr" in tokenize("LPR 是什么")


def test_stopword_filter_can_be_bypassed():
    """整条查询都是虚词时的退路 —— 滤干净了会返回空，而**空结果看起来和
    「知识库里没有」一模一样**。"""
    # 「的了的」全是虚词，滤完一个不剩
    assert tokenize("的了的") == []
    assert tokenize("的了的", drop_stopwords=False) != []


def test_content_words_are_never_stopwords():
    """**停用词表最怕的是手一抖把一个有信息的词也塞进去**，然后那个词
    永远搜不到，而且没有症状。

    这里钉住一批「看起来像虚词、其实是内容词」的例子。往 STOPWORDS 里加东西
    之前先想清楚：加了之后，含这个词的问题还能不能搜到。
    """
    from fa.retrieval.index import STOPWORDS

    for word in STOPWORDS:
        assert word not in {"少", "多", "存", "房", "税", "险", "钱", "年", "月"}


def test_content_words_survive_tokenization():
    tokens = tokenize("怎么少交点税")
    # 「怎么」被滤掉是对的（疑问词），但「少交」「交点」必须留下 ——
    # 它们是这条问题里唯一有信息的部分。
    assert "少交" in tokens
    assert "交点" in tokens
    assert "怎么" not in tokens


# --- BM25 ---------------------------------------------------------------


@pytest.fixture
def small_index():
    chunks = [
        Chunk("a.md", "a.md > 信用卡", "最低还款按全额计息，日息万分之五。"),
        Chunk("a.md", "a.md > 房贷", "等额本息每月还款额固定，总利息更多。"),
        Chunk("b.md", "b.md > 应急资金", "应急资金建议存三到六个月的开支。"),
    ]
    return SearchIndex().build(chunks, fingerprint="x")


def test_ranking_finds_the_right_chunk(small_index):
    top = small_index.rank("body", "最低还款")[0][0]
    assert small_index.chunks[top].title == "信用卡"


def test_rare_term_beats_common_term(small_index):
    """稀有词该压过常见词 —— 这是 IDF 存在的意义。"""
    top = small_index.rank("body", "应急资金 月")[0][0]
    assert small_index.chunks[top].title == "应急资金"


def test_unknown_query_returns_nothing(small_index):
    # 用一批和语料**一个字符都不重叠**的词，否则单字会打中别的块
    assert small_index.rank("body", "普洱茶 龙井") == []


def test_empty_index_is_not_an_error():
    assert SearchIndex().rank("body", "随便") == []
    assert search(SearchIndex(), "随便") == []


# --- 融合 ---------------------------------------------------------------


def test_title_and_body_are_separate_channels(small_index):
    """标题命中和正文命中是两种强度的证据，所以分两路而不是拼在一起。"""
    hits = search(small_index, "房贷", top_k=3)
    assert hits
    assert "标题" in hits[0].why or "正文" in hits[0].why


def test_fusion_score_depends_only_on_ranks():
    """**RRF 的全部意义：只用名次，不用分数。**

    两路 BM25 的分数不可比 —— 标题那一路文档短、IDF 分布和正文完全不是一回事，
    直接相加等于让分数大的那一路单方面决定排名。

    这里用两批**分数尺度完全不同**的语料验证：只要名次一样，
    融合出来的分数就必须一模一样。
    """
    def fused_top_score(texts: list[str]) -> float:
        chunks = [Chunk(f"{i}.md", f"{i}.md > 目标词", t) for i, t in enumerate(texts)]
        index = SearchIndex().build(chunks, fingerprint="")
        hits = search(index, "目标词", top_k=1)
        return hits[0].score

    short = fused_top_score(["目标词。", "别的。"])
    long = fused_top_score(["目标词。" * 200, "别的。" * 200])

    # 同样都是「两路都排第 1」，融合分必须相同，尽管两批语料的 BM25 分差很多。
    assert short == pytest.approx(long)
    assert short == pytest.approx(2 / (RRF_K + 1))


def test_a_hit_found_by_both_channels_outranks_one_found_by_one():
    """两路都命中比只命中一路更可信 —— 这是融合真正在做的事。

    断言写的是**具体哪两路**，不是「两路」。加第三路向量那次（第十一天）刻意
    选了后者会把这条测试留着不管的说法，但数数会放过一种情况：将来有人往
    `search` 里塞了第四路、同时把某一路砍了，数量还是 2 而内容已经错了。
    写死内容能逮住那个。

    （这条还是「向量路缺席时行为不变」的哨兵：索引上没挂 `dense`，所以这里
    永远只有标题和正文两路。真要挂了，这条会红。）
    """
    index = SearchIndex().build(
        [
            Chunk("a.md", "a.md > 无关", "目标词在这里，但标题里没有。"),
            Chunk("a.md", "a.md > 目标词小节", "正文里也提到了目标词。"),
        ],
        fingerprint="",
    )
    hits = search(index, "目标词", top_k=2)
    assert hits[0].chunk.title == "目标词小节"
    assert hits[0].channels == ("标题", "正文")


def test_results_are_stable_across_runs(small_index):
    """同一句话问两次必须拿到同样的顺序 —— 否则评测没法复现。"""
    a = [h.chunk.breadcrumb for h in search(small_index, "房贷 利息", top_k=3)]
    b = [h.chunk.breadcrumb for h in search(small_index, "房贷 利息", top_k=3)]
    assert a == b


def test_top_k_is_respected(small_index):
    assert len(search(small_index, "房贷 应急资金 信用卡", top_k=2)) <= 2


# --- 持久化与过期检测 ---------------------------------------------------


def test_index_round_trips(tmp_path, small_index):
    path = tmp_path / "idx.json"
    save(small_index, path)

    again = load(path)

    assert [c.breadcrumb for c in again.chunks] == [c.breadcrumb for c in small_index.chunks]
    assert again.rank("body", "最低还款")[0][0] == small_index.rank("body", "最低还款")[0][0]


def test_broken_index_degrades_to_empty(tmp_path):
    path = tmp_path / "idx.json"
    path.write_text("{ 这不是 JSON", encoding="utf-8")
    assert load(path).chunks == []


def test_fingerprint_changes_when_the_corpus_changes(tmp_path):
    write(tmp_path, "a.md", "# A\n\n第一版内容够长了。\n")
    before = corpus_fingerprint(tmp_path)

    write(tmp_path, "a.md", "# A\n\n第二版内容够长了。\n")

    assert corpus_fingerprint(tmp_path) != before


def test_fingerprint_includes_the_tokenize_scheme(tmp_path, monkeypatch):
    """**改了分词逻辑必须重建索引。**

    语料一个字节没动，指纹却必须变 —— 否则索引永远不重建，检索会静默地给出
    基于旧方案的排名。这是那种「测试全绿、线上悄悄不对」的场景。
    """
    write(tmp_path, "a.md", f"# A\n\n{LONG}\n")
    before = corpus_fingerprint(tmp_path)

    monkeypatch.setattr("fa.retrieval.index.SCHEME", "换了个分词方案")

    assert corpus_fingerprint(tmp_path) != before


def test_fingerprint_notices_an_edit_that_keeps_the_file_size(tmp_path):
    """改一个错别字、换一个词 —— 文件长度没变。

    第一版指纹用的是秒级的 `st_mtime`，同一秒内的这种修改它察觉不到，
    索引不会重建，检索继续用旧内容。**没有症状**，所以必须钉住。
    """
    write(tmp_path, "a.md", "# A\n\n第一版的内容写得足够长，能通过长度检查。\n")
    before = corpus_fingerprint(tmp_path)

    write(tmp_path, "a.md", "# A\n\n第二版的内容写得足够长，能通过长度检查。\n")

    assert corpus_fingerprint(tmp_path) != before


def test_load_or_build_rebuilds_when_the_corpus_is_edited(tmp_path):
    """改了文档却搜不到 —— 没有报错，看起来就像「知识库里没有」。"""
    write(tmp_path, "a.md", f"# A\n\n原来的内容。{LONG}\n")
    path = tmp_path / "idx.json"

    first = load_or_build(tmp_path, path)
    assert any("原来的内容" in c.text for c in first.chunks)

    write(tmp_path, "a.md", f"# A\n\n换成了完全不同的内容。{LONG}\n")

    second = load_or_build(tmp_path, path)
    assert any("完全不同" in c.text for c in second.chunks)


def test_load_or_build_reuses_a_fresh_index(tmp_path):
    write(tmp_path, "a.md", f"# A\n\n{LONG}\n")
    path = tmp_path / "idx.json"

    first = load_or_build(tmp_path, path)
    stamp = path.stat().st_mtime_ns
    second = load_or_build(tmp_path, path)

    assert path.stat().st_mtime_ns == stamp  # 没重写
    assert [c.text for c in second.chunks] == [c.text for c in first.chunks]


def test_empty_corpus_does_not_write_an_index(tmp_path):
    path = tmp_path / "idx.json"
    load_or_build(tmp_path, path)
    assert not path.exists()
