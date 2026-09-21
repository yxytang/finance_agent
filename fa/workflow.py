"""子 agent —— 上下文隔离。

## 为什么要有它，而不是让主 agent 自己做完

主 agent 和用户共用一份上下文，而那份上下文是有预算的。一个探索型的子任务
（「把过去一年每个类目的趋势都看一遍」）会在中间过程里塞进**大量原始数据**：
十几次查询的完整结果、每个类目的明细、几条被否定的线索。

那些中间结果在得出**结论**之后就再也没用了，但它们会一直留在上下文里，
而且后面每一轮都会被重新发一遍（还要付缓存的钱）。

子 agent 的做法：**用一份独立的上下文跑完，只把结论带回来。** 中间过程留在
它自己的 messages 里，用完即弃。

## 上下文隔离才是价值，不是并行

这两件事经常被混着说，但收益完全不同：

- 并行省的是**时间**，而一次子任务也就十几秒，省不出多少
- 隔离省的是**上下文**，而且**每一轮都在省** —— 主 agent 后面还要接着对话

所以判断要不要开子 agent，标准不是「能不能并行」，而是
**「这个子任务的中间过程有没有价值」**。

中间结果**本身就是答案**的时候（「把这 20 笔列出来」），开子 agent 反而有害：
结论就是中间结果，隔离之后信息反而丢了。

## 什么写死在代码里，什么交给模型

这是这一天真正要回答的问题。分界线是：**能力边界写死，使用决策交给模型。**

**写死在代码里**（这些不是判断题，是安全边界）：

- 子 agent 的工具集**只读** —— 它能查、能算、能检索，但不能改分类、不能写记忆
- 用**白名单**而不是黑名单：新加的工具**默认不给子 agent**，以后加了写工具
  也不会悄悄漏进去
- **不给它 `investigate` 自己** —— 递归的子 agent 是失控的乘数，而收益接近于零
- 步数上限比主 agent 低得多

**交给模型判断**：什么时候该开、子任务怎么切。

后者才真正需要判断力，而且模型手上有足够的信息（它知道主上下文里已经有什么）。
前者是硬约束，让模型去「判断」只会增加不确定性。
"""

from dataclasses import dataclass

from fa.config import SUBAGENT_MAX_STEPS
from fa.tools import build_tools
from fa.tools._util import Bill

# 子 agent 能用哪些工具。**白名单**：这里没列到的，它一律拿不到。
#
# 全是只读的。`correct_category` 会改分类缓存、`remember` 会写长期记忆 ——
# 那些改动需要用户确认，而子 agent 的中间过程用户看不到，**未经确认的写操作
# 不该从一个没人看的上下文里发生**。
SUBAGENT_TOOLS = (
    "describe_data",
    "query_transactions",
    "find_anomalies",
    "list_subscriptions",
    "search_knowledge",
)

SUBAGENT_PROMPT = """你是一个财务分析子助手。你被派去做一件**具体的、聚焦的**事，
做完把结论交回去就行。

## 你的输出会怎么被使用

主助手**只看得到你最后这一段话，看不到你的中间过程**。所以：

- 数字、时间范围、类目名、来源，全都要写进结论里。主助手看不到你查了什么，
  它只能引用你写下来的东西。
- **不要写「我查了 A，又查了 B」这种过程叙述** —— 那些步骤对结论没有贡献，
  写下来只是把主助手本来就看不到的东西又说了一遍，白占字数。
- 结论要能**独立成立**：主助手拿你这段话就能直接回答用户，不用再问一遍。

## 边界

- 你**只能读**：查账单、看异常、检索知识库。你不能改分类、不能记东西 ——
  那些要用户确认，不在你的授权范围里。
- 你**没有**再派子助手的工具。任务太大就说明情况然后停下，
  不要试图自己再拆一层。
- 查不到就直说查不到。**不要估、不要凭印象**。
"""


@dataclass(frozen=True)
class Delegation:
    """一次委派的账。

    把这份账回给主 agent，不是装饰 —— 它让「这次委派值不值」变成可见的东西。
    不记账的话，「子 agent 到底省没省上下文」永远是个说法，而不是一个数字。
    """

    question: str
    answer: str
    steps: int  # 子 agent 调了几轮工具
    context_chars: int  # 子 agent 的上下文一共攒了多少字符
    returned_chars: int  # 回传给主 agent 的有多少

    @property
    def saved_chars(self) -> int:
        """省下多少字符。可能是负的 —— 中间过程少而结论长时就会是负的。"""
        return self.context_chars - self.returned_chars

    def report(self) -> str:
        saved = self.saved_chars
        if saved >= 0:
            note = f"省下 {saved:,} 字符"
        else:
            note = f"**反而多了 {-saved:,} 字符**（这次不该开子 agent）"
        return (
            f"（子任务用了 {self.steps} 步、{self.context_chars:,} 字符的上下文，"
            f"回传 {self.returned_chars:,} 字符，{note}）"
        )


def readonly_tools(get_bill: Bill) -> list:
    """按白名单筛出子 agent 能用的工具。

    逐次过滤而不是单独构造一份：工具的定义只有一个地方（`tools/__init__.py`），
    这里只决定「哪些可以用」。两处定义就会漂移，然后有一处忘了改，
    子 agent 就拿到了不该拿的工具。
    """
    return [t for t in build_tools(get_bill=get_bill) if t.name in SUBAGENT_TOOLS]


def _context_chars(messages: list) -> int:
    """这些消息一共占了多少字符。

    用字符数而不是 token 数：token 要调分词器或者 API 才知道，而字符数和它
    高度相关，比较「隔离前后差多少」够用了 —— 而且它是本地可算的，能写进测试。
    """
    total = 0
    for message in messages:
        content = getattr(message, "content", "")
        total += len(content) if isinstance(content, str) else len(str(content))
        for call in getattr(message, "tool_calls", None) or []:
            total += len(str(call))
    return total


def run_subagent(
    question: str,
    *,
    get_bill: Bill,
    model=None,
    max_steps: int = SUBAGENT_MAX_STEPS,
) -> Delegation:
    """派一个子 agent 去做 `question`，只把结论拿回来。

    和主 agent 走**同一个 Session 循环** —— 只是工具集更小、提示词不同、
    步数上限更低。复用而不是另写一个循环，是因为那条「每个 tool_call 恰好一条
    ToolMessage」的不变量只该有一份实现。
    """
    # 延迟导入：agent 要 import tools，tools 那边的 delegate 工具又要 import
    # 这个模块，顶层互相 import 会转圈。
    from fa.agent import Session

    session = Session(
        tools=readonly_tools(get_bill),
        system_prompt=SUBAGENT_PROMPT,
        max_steps=max_steps,
        model=model,
    )
    answer = session.send(question)

    return Delegation(
        question=question,
        answer=answer,
        steps=sum(1 for m in session.messages if getattr(m, "tool_calls", None)),
        context_chars=_context_chars(session.messages),
        returned_chars=len(answer),
    )
