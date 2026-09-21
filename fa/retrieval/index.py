"""BM25 检索 —— 纯词法，因为 DeepSeek 没有 embedding 接口（查证过）。

这个限制不藏着：它意味着**换一种说法就搜不到**。第八天的 RAGAS
`context_recall` 分数就是它的实测代价，而不是找个好看的数字遮过去。

## 中文没有空格，所以先得解决「什么是词」

BM25 整套建立在「文档由词组成」这个前提上，中文直接把这个前提抽掉了。

这里用**字符 bigram**（「专项附加扣除」→ 专项/项附/附加/加扣/扣除），不用分词器：

- 不引依赖、不维护词典
- 中文 IR 的标准做法，召回和精度平衡最好
- **分词错误会直接引入检索错误**（把「个人所得」切成「个人/所得」，这个文档
  就再也搜不到了），而 bigram 是机械的，不会切错

**为什么还同时索引单字**：只用 bigram 的话，一个字的查询（「税」）在文档里
找不到任何对应 token，结果是零命中。加单字会让长查询多些噪音，但 BM25 的 IDF
会自然压住常见单字。两种失败的代价不对称：

    漏检（用户搜不到，而且不知道为什么）  >  误检（排在后面，看得见）

所以往「宁可多召回」那边倒。
"""

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from fa.retrieval.chunk import Chunk, load_corpus

# CJK 统一表意文字 + 扩展 A + 兼容区
_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]")

K1 = 1.5
B = 0.75

# 分词方案的版本号。**改了分词逻辑就必须改它**，否则 fingerprint 察觉不到索引
# 已经过期（语料文件没动），检索会静默地给出基于旧方案的排名。
SCHEME = "cjk-bigram+unigram+stopwords-v1"


# 停用词：中文虚词和疑问词。
#
# **为什么这个表非有不可**，而不是「有更好」：BM25 的 IDF 要在大语料上才分得开
# 「稀有因为有意义」和「稀有因为它是虚词、恰好没出现几次」。这份语料只有 60 个
# 块，一个疑问词的 df 可能是 5 —— 算出来的 idf 只比真正稀有的术语低一点点。
#
# 实测的后果：查「LPR 是什么」，含 LPR 的那个块得 7.37 分，输给了一个 9.75 分的
# 块 —— 而后者纯粹是靠「什么」「什」「么」赢的。
#
#     'lpr'  df=1  idf=3.71   ← 真正的信号
#     '什么'  df=5  idf=2.41   ← 疑问词
#
# 词表只收**在这个领域里绝不会有检索价值**的字词。像「少」「多」这种，
# 在「怎么少交点税」里是有意义的，就不该进表 —— 停用词表最怕的是手一抖
# 把一个有信息的词也塞进去，然后那个词永远搜不到了。
STOPWORDS = frozenset(
    """
    的 了 是 在 有 和 与 及 或 就 都 也 很 不 没 这 那 哪 什 么 怎 吗 呢 吧 啊
    把 被 给 让 从 对 而 但 之 其 此 该 些 个 我 你 他 她 它 们
    什么 怎么 为什么 哪些 哪个 多少 是不是 有没有 可以 需要 应该
    我们 你们 他们 这个 那个 一下 一些 的话 怎样 如何
    """.split()
)


def tokenize(text: str, *, drop_stopwords: bool = True) -> list[str]:
    """中文出 bigram（外加单字），拉丁字母数字出整词。

    「LPR 利率」会被切成 ["lpr", "利率"]。

    `drop_stopwords=False` 是给「整条查询都是虚词」这种情况留的退路 ——
    例如用户只打了「怎么样」，滤完之后一个 token 都不剩，检索会返回空，
    而那看起来和「知识库里没有」一模一样。
    """
    tokens: list[str] = []
    i = 0
    length = len(text)

    while i < length:
        char = text[i]
        if _CJK.match(char):
            j = i
            while j < length and _CJK.match(text[j]):
                j += 1
            run = text[i:j]
            tokens.extend(run)  # 单字：兜住「税」这种一个字的查询
            tokens.extend(run[k : k + 2] for k in range(len(run) - 1))
            i = j
        elif char.isalnum():
            j = i
            while j < length and text[j].isalnum():
                j += 1
            tokens.append(text[i:j].lower())
            i = j
        else:
            i += 1

    if drop_stopwords:
        tokens = [t for t in tokens if not _is_stopword(t)]
    return tokens


def _is_stopword(token: str) -> bool:
    """这个词该不该丢。

    除了查表，还有一条规则：**由两个字停用字组成的 bigram 也是停用词。**

    不补这条的话，「的了的」这种纯虚词串滤完还剩 `的了`、`了的` 两个 bigram ——
    它们没有任何检索价值，却会让「查询被滤空了」这个判断永远不成立，
    于是那条退路（滤空了就退回不过滤）形同虚设。
    """
    if token in STOPWORDS:
        return True
    return len(token) == 2 and all(ch in STOPWORDS for ch in token)


@dataclass
class _Field:
    """一路 BM25 需要的全部统计量。

    用倒排表（token → [(文档号, 词频)]）而不是每次遍历所有文档：块数上百之后
    后者会让每次查询都变成全表扫描，而检索是交互式的。
    """

    lengths: list[int] = field(default_factory=list)
    postings: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    df: Counter = field(default_factory=Counter)

    def add(self, doc_id: int, text: str) -> None:
        counts = Counter(tokenize(text))
        self.lengths.append(sum(counts.values()))
        for token, freq in counts.items():
            self.postings.setdefault(token, []).append((doc_id, freq))
            self.df[token] += 1

    @property
    def average_length(self) -> float:
        return sum(self.lengths) / len(self.lengths) if self.lengths else 0.0

    def score(self, query: str, n_docs: int) -> dict[int, float]:
        """返回 {文档号: 分数}。只用出现在查询里的 token，其余贡献为零。"""
        scores: dict[int, float] = {}
        avg = self.average_length
        if avg <= 0:
            return scores

        query_tokens = set(tokenize(query))
        if not query_tokens:
            # 整条查询都是虚词（比如用户只打了「怎么样」）。滤干净之后一个 token
            # 都不剩，检索会返回空 —— 而**空结果看起来和「知识库里没有」一模一样**。
            # 所以退回不过滤，宁可噪声大一点也不能静默地返回零结果。
            query_tokens = set(tokenize(query, drop_stopwords=False))

        for token in query_tokens:
            df = self.df.get(token)
            if not df:
                continue
            # 平滑过的 IDF：不写成 log(N/df)，因为那个式子对 df == N 的词会给出 0，
            # 让「每个文档都提到的词」彻底失去作用 —— 而那类词有时恰恰是
            # 唯一能把范围收窄的信号。
            idf = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
            for doc_id, freq in self.postings[token]:
                norm = 1 - B + B * self.lengths[doc_id] / avg
                scores[doc_id] = scores.get(doc_id, 0.0) + idf * freq * (K1 + 1) / (freq + K1 * norm)
        return scores


@dataclass
class SearchIndex:
    """两路 BM25：一路查标题（breadcrumb），一路查正文。

    分开而不是把 breadcrumb 拼进正文，是因为**标题命中和正文命中是两种不同强度
    的证据**。混合索引里一个正文偶然出现一次标题词的块，和真正的那个小节得分
    一样 —— 而它们该被区别对待。见 `hybrid.py` 怎么融合。
    """

    chunks: list[Chunk] = field(default_factory=list)
    title: _Field = field(default_factory=_Field)
    body: _Field = field(default_factory=_Field)
    # 建索引时语料的指纹，用来判断索引过期没有 —— 见 load_or_build。
    fingerprint: str = ""

    def build(self, chunks: list[Chunk], fingerprint: str = "") -> "SearchIndex":
        self.chunks = list(chunks)
        self.title = _Field()
        self.body = _Field()
        self.fingerprint = fingerprint
        for doc_id, chunk in enumerate(self.chunks):
            # 标题那一路把 source 也算进去 —— 用户会按文件名找东西
            # （「个税那篇里怎么说」）。
            self.title.add(doc_id, chunk.breadcrumb)
            self.body.add(doc_id, chunk.text)
        return self

    def rank(self, field_name: str, query: str) -> list[tuple[int, float]]:
        """按分数降序返回 [(文档号, 分数)]。"""
        field = self.title if field_name == "title" else self.body
        scores = field.score(query, len(self.chunks))
        return sorted(scores.items(), key=lambda kv: -kv[1])


# --- 持久化 -------------------------------------------------------------


def save(index: SearchIndex, path: Path) -> None:
    payload = {
        "fingerprint": index.fingerprint,
        "chunks": [
            {"source": c.source, "breadcrumb": c.breadcrumb, "text": c.text}
            for c in index.chunks
        ],
        "title": {"lengths": index.title.lengths, "postings": index.title.postings},
        "body": {"lengths": index.body.lengths, "postings": index.body.postings},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def load(path: Path) -> SearchIndex:
    """读回索引。文件不存在或坏了就返回空索引 —— 调用方重建立刻就能恢复，
    而抛异常会让「索引还没建」变成一次崩溃。"""
    if not path.is_file():
        return SearchIndex()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        chunks = [Chunk(**c) for c in payload["chunks"]]
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return SearchIndex()

    index = SearchIndex(chunks=chunks, fingerprint=payload.get("fingerprint", ""))
    for name in ("title", "body"):
        field_obj = _Field(
            lengths=payload[name]["lengths"],
            postings={k: [tuple(p) for p in v] for k, v in payload[name]["postings"].items()},
        )
        field_obj.df = Counter(
            {token: len(plist) for token, plist in field_obj.postings.items()}
        )
        setattr(index, name, field_obj)
    return index


def corpus_fingerprint(root: Path) -> str:
    """语料的指纹，用来判断索引是不是过期了。

    **指纹里必须带上分词方案本身**（`SCHEME`）。否则改了分词逻辑之后，语料文件
    一个字节没动，指纹不变，于是索引永远不重建 —— 而检索结果会静默地变错。
    这类「改了代码、测试全绿、线上悄悄不对」的场面，正是这个检查要防的。

    ## 为什么读内容，而不是只看修改时间

    一开始用的是「文件名 + 大小 + 修改时间」，理由是读内容太贵，而 stat 很便宜。
    那个优化是**错的**，而且错得不容易发现：

        同一秒（更准确地说，同一个文件系统时间戳 tick）内改一个长度不变的文件，
        大小和修改时间都不变 —— 指纹纹丝不动，索引不重建。

    这不是理论问题：`test_fingerprint_changes_when_the_corpus_changes` 就是这么
    红的，而且它是**间歇性**红的 —— 两次写盘恰好落在同一个 tick 里才复现。

    教训是：**这个检查存在的全部意义就是「不漏掉变化」。** 一个会漏的检查比没有
    检查更糟（你会以为它兜住了）。所以宁可读内容 —— 一份几十个文件的语料，
    读一遍是毫秒级的事，而漏一次的代价是检索持续返回旧结果且毫无症状。

    真要优化的话，该优化的是调用方（别每次查询都重算），而不是把正确性换掉。
    """
    digest = hashlib.sha1(SCHEME.encode("utf-8"))
    for path in sorted(root.rglob("*.md")):
        try:
            content = path.read_bytes()
        except OSError:
            continue
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def load_or_build(root: Path, path: Path) -> SearchIndex:
    """有新鲜的索引就用它，否则重建并存下来。

    这是检索层的唯一入口 —— 别的地方不该直接调 load 或 save，
    否则「什么时候该重建」这件事就会散成好几处，然后有一处忘了。
    """
    current = corpus_fingerprint(root)
    cached = load(path)

    if cached.chunks and cached.fingerprint == current:
        return cached

    index = SearchIndex().build(load_corpus(root), fingerprint=current)
    if index.chunks:
        save(index, path)
    return index
