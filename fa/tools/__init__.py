"""工具集。

`build_tools()` 是唯一入口：条件挂载全在这里，agent 主体不用知道某个工具
存不存在。第七天往这里塞 MCP 动态构造出来的工具 —— 到时候 agent.py 一行都不用
改，这就是这层存在的意义。

`get_bill` 是**取数函数**不是数据本身（见 `_util.Bill` 的说明）。默认走
`load_bill`：读 CSV + 贴类目（只走规则和缓存两层，不调 LLM）。
第七天换成从 MCP server 取，就是换这一个函数的事。
"""

from langchain_core.tools import BaseTool

from fa.permissions import Confirm
from fa.tools._util import Bill, load_bill
from fa.tools.anomaly import build_anomaly_tools
from fa.tools.categorize import build_categorize_tools
from fa.tools.knowledge import build_knowledge_tools
from fa.tools.memory import build_memory_tools
from fa.tools.query import build_query_tools
from fa.tools.skills import build_skill_tools


def build_tools(
    confirm: Confirm | None = None, get_bill: Bill | None = None
) -> list[BaseTool]:
    """构造这个 Session 能用的全部工具。

    `confirm is None` 表示**全部放行**（`python -m fa --yes` 走的就是这条）。
    """
    bill = get_bill if get_bill is not None else load_bill
    return [
        *build_query_tools(bill),
        *build_anomaly_tools(bill),
        *build_knowledge_tools(),
        *build_categorize_tools(confirm),
        *build_memory_tools(),
        *build_skill_tools(),
    ]
