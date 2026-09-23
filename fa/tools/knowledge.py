"""把知识库检索包成工具。

这个工具和 `query_transactions` 是**两条完全不同的路**，模型必须自己判断走哪条：

    问「上个月外卖花了多少」  →  query_transactions（确定性，数字从账单算）
    问「房贷利息能抵多少个税」 →  search_knowledge（检索，答案在知识库里）

判断错的表现很隐蔽：拿账单数据去回答政策问题，或者反过来。前者的输出会是一个
看起来合理但和问题无关的数字。

两边都不适用时（比如「我该不该买房」），诚实的回答是「这取决于……」+ 把相关
因素列出来，而不是硬找一个工具去凑。
"""

from functools import lru_cache

from langchain_core.tools import BaseTool, tool

from fa.retrieval import load_or_build, render, search
from fa.tools._util import truncate

# 一次返回多少个块。多了会把上下文塞满，而且后几名基本是噪音 ——
# 融合之后第 5 名往后分数掉得很快。
DEFAULT_TOP_K = 4


def _paths():
    """从 config **在调用时**读，测试要能把语料换到临时目录。"""
    from fa import config

    return config.KNOWLEDGE_DIR, config.KNOWLEDGE_INDEX


@lru_cache(maxsize=1)
def _vector_parts():
    """向量那两件东西（embedder + 向量库）。没配就返回 `(None, None)`。

    **进程内只开一次。** chromadb 的 PersistentClient 每次打开都要碰磁盘，而
    `load_or_build` 是每检索一次就调一次的 —— 不缓存的话每次提问都重开一遍。

    没配 `RAG_API_KEY` 时返回 `(None, None)`，于是 `load_or_build` 只走词法
    两路，和加向量之前**逐字节一样**。这是降级路径，不是错误路径。

    embedder 外面包了一层缓存（`CachedEmbedder`）：语料改一个字节，原来 83 个
    子块全部重打 embedding，现在只重打变的那几个。

    **这里顺手打一行缓存后端。** 用一个 `lru_cache` 的函数做打印是有点怪的，
    但这是「进程内只发生一次」的地方，而缓存退回了哪种后端**必须让人看见** ——
    静默退回会让「我配了 Redis 怎么没快」变成一个查不出来的问题。
    （同样的取舍见 `hybrid._semantic_ranking` 的只喊一次。）

    权衡和 `_util.load_bill` 一样：这份东西只读、进程内共享。
    **测试要换语料目录的话，得先 `_vector_parts.cache_clear()`**。
    """
    from fa import config
    from fa.retrieval.cache import CachedEmbedder, open_cache
    from fa.retrieval.dense import HttpEmbedder, open_store

    settings = config.rag_settings()
    if settings is None:
        return None, None

    cache = open_cache()
    print(f"检索缓存：{cache.describe()}")
    return CachedEmbedder(HttpEmbedder(settings), cache), open_store(config.CHROMA_DIR)


def build_knowledge_tools() -> list[BaseTool]:
    @tool
    def search_knowledge(query: str) -> str:
        """查财务知识库 —— 政策、规则、概念类的问题从这里找答案。

        **什么时候用它**：问的是「一般规则」而不是「我的账单」。
        比如「专项附加扣除有哪些」「信用卡分期真实利率多少」「应急资金该存
        几个月」「提前还房贷划不划算」。

        **什么时候不该用它**：问的是「我花了多少」「我有没有被多扣钱」——
        那些走 query_transactions / find_anomalies，答案得从账单里算出来。

        query 写**整句**还是压成关键词都行，不用特意改写。这个检索是混合的：
        两路词法（标题 + 正文）加一路语义向量，再叠一层重排 —— 口语问法也
        命中得了。

        （第十一天量过：给这个检索加多查询改写、HyDE 之类的改写层，**一个字
        都不变**。向量和重排已经把「换个说法」这个洞补上了，改写没有东西可修。
        所以这里不劝你改写。）

        附带说明：知识库里的金额标准以**撰写时**的政策为准，回答时如果涉及
        具体数字，应该提醒用户以当年最新政策为准。
        """
        knowledge_dir, index_path = _paths()
        embedder, store = _vector_parts()
        index = load_or_build(
            knowledge_dir, index_path, embedder=embedder, store=store
        )

        if not index.chunks:
            return (
                f"知识库是空的（{knowledge_dir} 里没有可检索的内容）。"
                "这不要紧，但涉及政策类的问题就直接说明查不到，不要凭记忆回答。"
            )

        hits = search(index, query, top_k=DEFAULT_TOP_K)
        if not hits:
            return (
                f"知识库里没有和「{query}」相关的内容。\n"
                "换个说法或者用更通用的关键词再试一次；如果还是没有，"
                "就直接告诉用户知识库里没有这一块，**不要凭记忆编**。"
            )

        return truncate(
            render(hits)
            + "\n\n（以上是知识库原文。引用时说明出自哪一篇、哪一节，"
            "涉及具体金额标准的要提醒以当年政策为准。）"
        )

    return [search_knowledge]
