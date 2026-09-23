"""`search_knowledge` 工具的接线（`fa/tools/knowledge.py`）。

这个文件此前一条测试都没有。加它是因为第十一天给它挂上了向量路 —— 而**接线
错了不会报错**，只会静默地退回纯 BM25：检索照跑、结果照出，只是质量悄悄回到
加向量之前。

另外两条测的是那个 docstring —— 它就是提示词，写错一句话会直接改变模型的
行为，而没有任何报错。
"""

import pytest

from fa import config
from fa.tools.knowledge import _vector_parts, build_knowledge_tools


@pytest.fixture(autouse=True)
def _fresh_vector_cache():
    """每个测试都从「没缓存过」开始。

    `_vector_parts` 是 `lru_cache` 的 —— 进程内只开一次向量库。缓存住的正是
    「用哪个 key、开哪个库」，所以测试之间不隔离的话，一个没配 key 的测试会
    拿到上一个测试缓存下来的 embedder。
    """
    _vector_parts.cache_clear()
    yield
    _vector_parts.cache_clear()


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    """把知识库换到临时目录 —— 不碰仓库里的真语料。

    ⚠️ **每节正文都得超过 `MIN_CHUNK_CHARS`（30 字）**，否则会被
    `chunk_markdown` 当成「只有标题没有内容」的占位块丢掉，然后你查什么都
    是「知识库里没有」。这个坑我踩了两次。
    """
    (tmp_path / "税.md").write_text(
        "# 个税\n\n## 累计预扣法\n\n"
        "每月到手工资不一样，是因为个税按累计预扣法算，"
        "收入累计到一定档位，税率会跳一档，所以后几个月扣得多。\n"
        "\n## 起征点\n\n"
        "综合所得的基本减除费用是每月五千元，这就是通常说的「起征点」，"
        "从二零一八年开始执行，每年还会加上专项附加扣除。\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "KNOWLEDGE_DIR", tmp_path)
    monkeypatch.setattr(config, "KNOWLEDGE_INDEX", tmp_path / ".index.json")
    return tmp_path


def tool():
    return next(t for t in build_knowledge_tools() if t.name == "search_knowledge")


# --- 接线 ---------------------------------------------------------------


def test_without_a_key_the_dense_channel_is_simply_absent(monkeypatch):
    """没配 key → `(None, None)` → 只有词法两路。

    这是**正常路径**，不是错误路径：agent 照常跑，只是没有向量加持。做成报错
    的话，一个只想问账单的机器会被一个它没用到的功能拦下来。
    """
    monkeypatch.delenv("RAG_API_KEY", raising=False)

    assert _vector_parts() == (None, None)


def test_the_vector_parts_are_cached(monkeypatch):
    """进程内只开一次向量库 —— chromadb 每次打开都要碰磁盘，而检索是高频的。"""
    monkeypatch.delenv("RAG_API_KEY", raising=False)
    _vector_parts.cache_clear()

    first = _vector_parts()
    assert _vector_parts() is first or _vector_parts() == first

    info = _vector_parts.cache_info()
    assert info.misses == 1, f"没有命中缓存：{info}"


def test_the_tool_returns_hits_without_any_vector_setup(corpus, monkeypatch):
    """没配 key 也照样检索得到东西 —— 这条兜住「接线把工具整个弄坏了」。"""
    monkeypatch.delenv("RAG_API_KEY", raising=False)

    out = tool().invoke({"query": "起征点是多少"})

    assert "起征点" in out
    assert "个税" in out


def test_an_empty_corpus_says_so_instead_of_pretending(corpus, monkeypatch):
    """空语料要如实说，而且提醒别凭记忆答 —— 这条是原有的行为，别被接线弄丢。"""
    monkeypatch.delenv("RAG_API_KEY", raising=False)
    for path in corpus.rglob("*.md"):
        path.unlink()

    out = tool().invoke({"query": "起征点是多少"})

    assert "空的" in out
    assert "不要凭记忆" in out


# --- docstring（它就是提示词）------------------------------------------


def test_the_docstring_no_longer_steers_the_model_toward_keywords():
    """**这句话现在是错的，而且它在帮倒忙。**

    原文写着「这个检索是纯词法的（没有语义向量），换个说法就搜不到」，所以
    「query 写关键词通常更准」。加了语义那一路之后第一句不成立了；第二句更糟
    —— 它在**劝模型写关键词、别写自然语句**，而混合检索本来不需要这个迁就。

    提示词里留一句假话不会报错，只会让模型按一句过时的约束行事。
    """
    text = tool().description

    assert "纯词法" not in text
    assert "没有语义向量" not in text
    assert "换个说法就搜不到" not in text


def test_the_docstring_still_says_when_not_to_use_it():
    """改的时候容易把「什么时候别用它」一起删掉 —— 那是这个 section 存在的
    一半理由（见模块 docstring：路由错了，输出会是一个看着合理的无关数字）。"""
    text = tool().description

    assert "什么时候不该用它" in text
    assert "query_transactions" in text
