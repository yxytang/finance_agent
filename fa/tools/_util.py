"""工具层共用的小东西。"""

from functools import lru_cache

from fa.config import MAX_TOOL_OUTPUT, TRANSACTIONS_CSV
from fa.ingest import load_transactions
from fa.models import Money, Transaction


def truncate(text: str) -> str:
    """超长就截断，并且**留话说明截掉了多少**。

    留这句很重要：模型看到结果断在半句上，会以为那就是全部，
    然后基于一个残缺的观察继续下结论。
    """
    if len(text) <= MAX_TOOL_OUTPUT:
        return text
    return text[:MAX_TOOL_OUTPUT] + f"\n…[已截断，原文共 {len(text)} 字符]"


@lru_cache(maxsize=1)
def load_bill() -> tuple[Transaction, ...]:
    """读账单，**进程内只读一次**。

    每次构造 Session 都会走到这里，而这份 CSV 有 1000 多行 —— 不缓存的话
    测试里几十次 Session 构造就要重复解析几十遍。文件在进程生命周期里不会变，
    读一次就够。

    第七天接上 MCP 之后，账单会改从 server 取，这个函数就该退休了。

    返回 tuple 而不是 list：它是共享的缓存对象，**调用方拿到 list 很容易顺手
    改一改**，然后所有会话都跟着变，而且不报错。
    """
    return tuple(load_transactions(TRANSACTIONS_CSV))


def format_value(value: Money, agg: str) -> str:
    """按 agg 决定怎么显示这个数。

    笔数不能写成「24.00 笔」—— 一个带小数点的笔数会让读到它的人怀疑整个
    结果的可靠程度。
    """
    if agg == "count":
        return f"{int(value):,} 笔"
    return f"{value:,.2f}"
