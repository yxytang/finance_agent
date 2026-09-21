"""把知识库检索包成工具。

这个工具和 `query_transactions` 是**两条完全不同的路**，模型必须自己判断走哪条：

    问「上个月外卖花了多少」  →  query_transactions（确定性，数字从账单算）
    问「房贷利息能抵多少个税」 →  search_knowledge（检索，答案在知识库里）

判断错的表现很隐蔽：拿账单数据去回答政策问题，或者反过来。前者的输出会是一个
看起来合理但和问题无关的数字。

两边都不适用时（比如「我该不该买房」），诚实的回答是「这取决于……」+ 把相关
因素列出来，而不是硬找一个工具去凑。
"""

from langchain_core.tools import BaseTool, tool

from fa.retrieval import load_or_build, render, search
from fa.tools._util import truncate

# 一次返回多少个块。多了会把上下文塞满，而且后几名基本是噪音 ——
# RRF 融合之后第 5 名往后分数掉得很快。
DEFAULT_TOP_K = 4


def _paths():
    """从 config **在调用时**读，测试要能把语料换到临时目录。"""
    from fa import config

    return config.KNOWLEDGE_DIR, config.KNOWLEDGE_INDEX


def build_knowledge_tools() -> list[BaseTool]:
    @tool
    def search_knowledge(query: str) -> str:
        """查财务知识库 —— 政策、规则、概念类的问题从这里找答案。

        **什么时候用它**：问的是「一般规则」而不是「我的账单」。
        比如「专项附加扣除有哪些」「信用卡分期真实利率多少」「应急资金该存
        几个月」「提前还房贷划不划算」。

        **什么时候不该用它**：问的是「我花了多少」「我有没有被多扣钱」——
        那些走 query_transactions / find_anomalies，答案得从账单里算出来。

        query 写**关键词**还是写整句都行，但关键词通常更准：这个检索是纯词法的
        （没有语义向量），换个说法就搜不到。搜「个税 专项附加扣除 房贷利息」
        比搜「我想知道我这种情况能少交多少税」命中率高得多。

        附带说明：知识库里的金额标准以**撰写时**的政策为准，回答时如果涉及
        具体数字，应该提醒用户以当年最新政策为准。
        """
        knowledge_dir, index_path = _paths()
        index = load_or_build(knowledge_dir, index_path)

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
