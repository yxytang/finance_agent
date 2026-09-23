"""检索基线不许掉。

**这是 CI 上唯一一条碰真实语料的检索测试**（别的都用 `make_bill` 那种自造
fixture）。故意如此：要钉住的正是「词法那两路在真语料上的表现」——

    python -m eval.retrieval --mode bm25

那条命令的输出就是这里断言的数字。`--mode full` 跑不了 CI（要 API key 和网络），
所以**向量和重排的增益不在这个文件里守**，靠的是 `eval/retrieval.py` 手工跑。

## 为什么只钉下界，不钉等号

- 钉下界（`>=`）能抓住**回退**，而不会在有人改好它的时候假报警。
- 转述组那个 1/5 是**已知的短板**，钉住它是为了防止它悄悄变成 0 ——
  不是因为它是个好数字。

## 为什么不会因为日期漂

语料是提交进仓库的（`knowledge/*.md`），不是生成的，所以这几个数只跟
分块方案和分词方案有关。改了那两样就得同时改这里的期望值 ——
**那正是应该发生的事情**（`fa/retrieval/index.py` 的 `SCHEME` 注释说的就是它）。
"""

import pytest

from fa.config import KNOWLEDGE_DIR, KNOWLEDGE_INDEX
from fa.retrieval import load_or_build, search
from eval.retrieval import LITERAL, PARAPHRASED


@pytest.fixture(scope="module")
def index():
    """真语料 + 词法两路。

    **不带 embedder** —— 这条测试必须在装了 chromadb 和没装 chromadb 的机器上
    给出同样的结果。带了的话，同一份断言在两种机器上会不一样。
    """
    return load_or_build(KNOWLEDGE_DIR, KNOWLEDGE_INDEX)


def file_at_1(index, cases) -> int:
    hits = 0
    for case in cases:
        got = search(index, case.question, top_k=1)
        if got and got[0].chunk.source == case.source:
            hits += 1
    return hits


def test_the_corpus_is_not_empty(index):
    """前提前提 —— 语料没了的话下面两条会「通过」（0 >= 0 那种）。"""
    assert len(index.chunks) > 50


def test_the_literal_group_does_not_regress(index):
    """字面问法一直是强项（14/15）。它掉下来就是回退，不是「波动」。"""
    assert file_at_1(index, LITERAL) >= 14


def test_the_paraphrase_group_does_not_collapse(index):
    """**这条钉的是一个已知的短板，不是在夸它。**

    纯词法在换说法上只有 1/5 —— 加向量那件事就是因为这个才做的。钉住它，
    是为了让「连这 1 分都没了」会红：那种回退通常意味着分词或分块被改坏了，
    而它不会以任何别的方式表现出来（检索照跑，只是搜不到）。
    """
    assert file_at_1(index, PARAPHRASED) >= 1
