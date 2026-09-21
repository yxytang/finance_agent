"""工具集。

`build_tools()` 是唯一入口：条件挂载全在这里，agent 主体不用知道某个工具
存不存在。第七天往这里塞 MCP 动态构造出来的工具 —— 到时候 agent.py 一行都不用
改，这就是这层存在的意义。

`txns` 是给测试用的注入口。不传就读 `data/transactions.csv`（读一次，进程内
缓存）。第七天账单改从 MCP server 取之后，这个参数会变成「从 server 拉回来
的那一份」。
"""

from langchain_core.tools import BaseTool

from fa.models import Transaction
from fa.permissions import Confirm
from fa.tools._util import load_bill
from fa.tools.anomaly import build_anomaly_tools
from fa.tools.query import build_query_tools
from fa.tools.skills import build_skill_tools


def build_tools(
    confirm: Confirm | None = None, txns: tuple[Transaction, ...] | None = None
) -> list[BaseTool]:
    """构造这个 Session 能用的全部工具。

    `confirm` 现在还没人用（第一个会写数据的工具在第四天）。参数先留着，
    免得那时候要改所有调用点。
    """
    bill = load_bill() if txns is None else txns
    return [
        *build_query_tools(bill),
        *build_anomaly_tools(bill),
        *build_skill_tools(),
    ]
