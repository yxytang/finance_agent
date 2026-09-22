"""评测代码本身的自检。

评测的价值全在「数字可信」上，而数字不可信最常见的来源不是算错，是**输入悄悄
坏了** —— 题库里有一条参考答案是空的、`source` 指向一个不存在的文件、
采集脚本静默地少记了几道题。这些都不会让任何东西报错。

所以这个文件测的不是「指标算得对不对」（那是 ragas 的事），而是
**「喂给它的东西是完好的吗」**。
"""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from eval.collect import Recorder, ask
from eval.qa_cases import ALL, LITERAL, PARAPHRASED
from eval.ragas_run import sanity_problem, to_dataset
from fa.config import KNOWLEDGE_DIR

# ragas 在 `eval` extra 里，CI 只装 `.[dev]`（ragas 会带进两百多兆的传递依赖）。
#
# 用 `find_spec` 而不是 `pytest.importorskip`：后者的行为随版本变过 ——
# 它现在只对 `ModuleNotFoundError` 跳过，对别的 `ImportError` 会直接失败
# （那是有意的，用来区分「没装」和「装了但坏了」）。但这里我想要的是一个
# **不依赖版本细节**的判断，而且看代码的人一眼就知道在判什么。
HAS_RAGAS = importlib.util.find_spec("ragas") is not None
requires_ragas = pytest.mark.skipif(not HAS_RAGAS, reason="需要 eval extra（CI 不装）")


# --- 题库 ---------------------------------------------------------------


def test_every_case_has_a_hand_written_reference():
    """参考答案是**手工核对**写的，不是生成的 —— 这是整个评测的立足点。

    RAGAS 的 `context_recall` / `context_precision` 拿它当真值。真值是空的或者
    是模型生成的话，评测就变成了「两个模型的口味差多少」，而且是自证的：
    检索得好、答案也答得好，分数虚高而没人知道。
    """
    for case in ALL:
        assert case.reference.strip(), f"{case.question} 没有参考答案"
        assert len(case.reference) >= 20, f"{case.question} 的参考答案太短，像是占位"


def test_questions_are_unique():
    """重复的问题会让某一类知识被重复计权，而总分看起来还挺正常。"""
    questions = [c.question for c in ALL]
    assert len(questions) == len(set(questions))


def test_every_source_points_at_a_real_document():
    """`source` 指向不存在的文件时，findings 里那张「按文档看 recall」的表
    会多出一行永远不会被命中的文档 —— 而它看起来像检索的锅。"""
    names = {p.name for p in KNOWLEDGE_DIR.glob("*.md")}
    for case in ALL:
        assert case.source in names, f"{case.source} 不在 knowledge/ 里"


def test_both_groups_are_populated():
    """两组分开报才有意义。某一组空掉的话，那张对照表会静默地少一半。"""
    assert len(LITERAL) >= 10
    assert len(PARAPHRASED) >= 3


def test_the_paraphrased_group_really_avoids_literal_overlap():
    """转述组的意义就是「文档里一个原词都没有」。

    混进一条字面题的话，那一组的分数会虚高 —— 而**虚高的方向恰好对我们有利**，
    所以它特别容易被放过。
    """
    from fa.retrieval.index import tokenize

    corpus = "\n".join(p.read_text(encoding="utf-8") for p in KNOWLEDGE_DIR.glob("*.md"))
    for case in PARAPHRASED:
        # 转述题里最长的几个内容词不该原样出现在语料里
        terms = [t for t in set(tokenize(case.question)) if len(t) >= 3]
        hit = [t for t in terms if t in corpus]
        assert len(hit) <= 1, f"「{case.question}」里有 {hit} 原样出现在语料里，不算转述"


# --- 采集 ---------------------------------------------------------------


def test_recorder_records_and_restores():
    """劫持要能还原。不还原的话，同一个进程里后续的检索都会被它悄悄改道。"""
    import fa.tools.knowledge as module

    original = module.search
    recorder = Recorder()
    recorder.install()
    assert module.search is not original

    recorder.restore()
    assert module.search is original


def test_recorder_captures_what_the_retriever_returned(monkeypatch):
    """记的是**检索器返回的对象**，不是渲染出来的文本。

    从渲染文本反解等于在测渲染器；而且格式一改，解析就静默地返回空 ——
    然后所有题的 `retrieved_contexts` 都变成空，分数全塌，而没有任何报错。
    """
    import fa.tools.knowledge as module
    from fa.retrieval.chunk import Chunk
    from fa.retrieval.hybrid import Hit

    hits = [
        Hit(
            chunk=Chunk("个税基础.md", "个税基础.md > 起征点", "每月 5000 元。"),
            score=1.0,
            channels=("标题",),
        )
    ]
    monkeypatch.setattr(module, "search", lambda index, query, **kwargs: hits)

    recorder = Recorder()
    recorder.install()
    try:
        module.search(None, "个税 起征点")
    finally:
        recorder.restore()

    assert recorder.calls == [
        {
            "query": "个税 起征点",
            "chunks": [
                {
                    "source": "个税基础.md",
                    "breadcrumb": "个税基础.md > 起征点",
                    "text": "每月 5000 元。",
                }
            ],
        }
    ]


def test_ask_retries_only_on_the_content_filter(monkeypatch):
    """只对**内容过滤**重试。

    对别的失败也重试的话，「模型答不出来」会被重试成一个看起来正常的答案 ——
    而那掩盖的正是我们想量的东西。
    """
    calls = []

    class _Session:
        def __init__(self, **kwargs):
            calls.append(1)

        def send(self, question):
            return "正常答案" if len(calls) >= 2 else "这次回复被内容过滤拦掉了"

    monkeypatch.setattr("eval.collect.Session", _Session)

    answer, attempts = ask("随便问")

    assert answer == "正常答案"
    assert attempts == 1  # 重试了一次
    assert len(calls) == 2


def test_ask_gives_up_eventually(monkeypatch):
    """一直撞过滤时不能无限重试 —— 那会挂在这儿烧钱。"""
    class _Session:
        def __init__(self, **kwargs):
            pass

        def send(self, question):
            return "被内容过滤拦掉了"

    monkeypatch.setattr("eval.collect.Session", _Session)
    monkeypatch.setattr("eval.collect.time.sleep", lambda _: None)

    answer, attempts = ask("随便问", attempts=2)

    assert "过滤" in answer
    assert attempts == 1  # attempts-1 = 最后一次的下标


# --- 组装数据集 ---------------------------------------------------------


@requires_ragas
def test_dataset_maps_the_ragas_fields():
    """字段映射错了的话 ragas 会用空上下文去打分，而分数看起来只是「偏低」。

    ragas 是 `eval` extra，CI 不装。所以这条在 CI 上会跳过 —— 但**同文件里那些
    不需要 ragas 的检查照跑**，因为 `eval.ragas_run` 的 ragas import 是懒的。
    """
    samples = [
        {
            "question": "问",
            "reference": "参考答案",
            "response": "模型答案",
            "retrieved_contexts": ["块1", "块2"],
            "group": "literal",
        }
    ]

    sample = to_dataset(samples).samples[0]

    assert sample.user_input == "问"
    assert sample.reference == "参考答案"
    assert sample.response == "模型答案"
    assert sample.retrieved_contexts == ["块1", "块2"]


@requires_ragas
def test_no_context_becomes_a_placeholder_not_an_empty_list():
    """一块都没检索到时也要给 ragas 一个非空列表。

    空列表可能让它算出 0 分、也可能让它算不出分然后**静默地跳过这一题** ——
    后者会让总分看起来比实际好，因为最难的那道题被跳过了。
    """
    samples = [
        {
            "question": "问",
            "reference": "参考答案",
            "response": "答",
            "retrieved_contexts": [],
            "group": "literal",
        }
    ]

    contexts = to_dataset(samples).samples[0].retrieved_contexts
    assert contexts and contexts != []


def test_importing_the_eval_module_does_not_drag_in_ragas():
    """ragas 的 import 必须是懒的。

    放顶层的话，CI（只装 `.[dev]`）一 import 就炸 —— 而**这一天最重要的东西
    （下面那道防呆、题库自检、采集器检查）根本不需要 ragas**。
    依赖的重量不该拖累用不到它的测试。
    """
    import importlib
    import sys

    # 先把它从模块表里摘掉，再看重新 import 会不会把它带回来
    for name in list(sys.modules):
        if name == "ragas" or name.startswith("ragas."):
            del sys.modules[name]

    importlib.reload(importlib.import_module("eval.ragas_run"))

    assert not any(n == "ragas" or n.startswith("ragas.") for n in sys.modules), (
        "import eval.ragas_run 把 ragas 带进来了 —— CI 上会 ImportError"
    )


# --- 跑之前的防呆 -------------------------------------------------------


def test_sanity_check_catches_headings_without_body_text():
    """**这条检查是被一个真 bug 逼出来的。**

    第一版采集只记了 `source` 和 `breadcrumb`，没记块正文 —— 于是喂给 RAGAS 的
    是一堆光秃秃的小节名，三个指标全部接近 0。

    危险的地方在于**那张表看起来像个真实（很差的）结果**：没有报错、进度条正常
    走完、分数落在合理区间。发现它靠的是另一个独立算出来的数字（本地统计
    agent 有没有检索到正确文档，结果是 17/17）—— 两个数对不上。
    """
    broken = [{"retrieved_contexts": ["[个税基础.md] 个税基础.md > 起征点与应纳税所得额"]}]
    assert sanity_problem(broken)

    healthy = [{"retrieved_contexts": ["个税基础.md > 起征点\n" + "正文" * 200]}]
    assert sanity_problem(healthy) is None


def test_sanity_check_uses_per_chunk_length_not_per_sample():
    """**分母错了，阈值就没意义。**

    第一版算的是「每题所有块的总长」，于是 4 个 40 字的标题加起来 160 字、
    轻松越过阈值 —— 一个抓不住它要抓的东西的检查。
    """
    many_short_chunks = [{"retrieved_contexts": ["[x.md] x.md > 小节名"] * 20}]
    assert sanity_problem(many_short_chunks)


def test_sanity_check_handles_the_degenerate_inputs():
    from eval.ragas_run import sanity_problem

    assert sanity_problem([])
    assert sanity_problem([{"retrieved_contexts": []}])
