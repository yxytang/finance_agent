"""工具层共用的小东西。"""

from collections.abc import Callable
from functools import lru_cache

from fa.categorize import categorize
from fa.config import MAX_TOOL_OUTPUT, TRANSACTIONS_CSV
from fa.ingest import load_transactions
from fa.models import Money, Transaction

# 工具拿的是**取数函数**，不是数据本身。
#
# 一开始传的是元组，看起来更简单，但用户纠正一条分类之后 agent 手里的账单还是
# 旧的 —— 「纠正了却没变化」比根本不能纠正更让人困惑。传函数就没有这条缝。
#
# 顺带，这也正好是第七天接 MCP 的接缝：到时候换掉取数函数就行，工具一行不改。
Bill = Callable[[], tuple[Transaction, ...]]


def truncate(text: str) -> str:
    """超长就截断，并且**留话说明截掉了多少**。

    留这句很重要：模型看到结果断在半句上，会以为那就是全部，
    然后基于一个残缺的观察继续下结论。
    """
    if len(text) <= MAX_TOOL_OUTPUT:
        return text
    return text[:MAX_TOOL_OUTPUT] + f"\n…[已截断，原文共 {len(text)} 字符]"


def format_value(value: Money, agg: str) -> str:
    """按 agg 决定怎么显示这个数。

    笔数不能写成「24.00 笔」—— 一个带小数点的笔数会让读到它的人怀疑整个
    结果的可靠程度。
    """
    if agg == "count":
        return f"{int(value):,} 笔"
    return f"{value:,.2f}"


@lru_cache(maxsize=1)
def load_bill() -> tuple[Transaction, ...]:
    """读账单、贴上类目，进程内只做一次。

    **只走规则和缓存两层，不调 LLM。** 查询是交互式的，不能让用户等一次分类；
    LLM 那一层由 `python -m data.categorize` 批量跑，跑完写进缓存，这里自动
    就用上了。

    缓存是必要的：每次构造工具集都会走到这里，而这份 CSV 一千多行。
    """
    raw = load_transactions(TRANSACTIONS_CSV)
    tagged, _ = categorize(raw, use_llm=False)
    return tuple(tagged)


def reload_bill() -> tuple[Transaction, ...]:
    """清缓存重读。

    用户刚纠正了一条分类时必须调它 —— 否则纠正要等到下次重启才生效，而
    「纠正了却没变化」比根本不能纠正更让人困惑。
    """
    load_bill.cache_clear()
    return load_bill()
