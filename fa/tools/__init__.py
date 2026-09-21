"""工具集。

`build_tools()` 是唯一入口：条件挂载全在这里，agent 主体不用知道某个工具
存不存在。MCP 那些动态构造出来的工具就是从这儿挂进去的 ——
agent.py 一行都没改，这就是这层存在的意义。

`get_bill` 是**取数函数**不是数据本身（见 `_util.Bill` 的说明）。默认走
`load_bill`：读 CSV + 贴类目（只走规则和缓存两层，不调 LLM）。
"""

import atexit
from functools import lru_cache

from langchain_core.tools import BaseTool

from fa.permissions import Confirm
from fa.tools._util import Bill, load_bill
from fa.tools.anomaly import build_anomaly_tools
from fa.tools.categorize import build_categorize_tools
from fa.tools.delegate import build_delegate_tools
from fa.tools.knowledge import build_knowledge_tools
from fa.tools.memory import build_memory_tools
from fa.tools.query import build_query_tools
from fa.tools.skills import build_skill_tools

# MCP 工具的挂载前缀。**必须加前缀**：MCP server 不知道我们内部有哪些工具，
# 名字撞上时 LangChain 会静默地用一个盖掉另一个 —— 而那种失败看起来是
# 「某个内置工具忽然不好用了」，很难联想到是外部 server 干的。
MCP_PREFIX = "ledger__"


@lru_cache(maxsize=1)
def _mcp_tools() -> tuple[BaseTool, ...]:
    """连一次账单 server，把它暴露的工具全挂上。

    **进程内只连一次**：每次都起一个子进程的话，测试里几十次 Session 构造
    就要起几十个进程。客户端是长连接，一个就够。

    连不上就返回空元组（`mount_tools` 会打到 stderr）—— agent 还能用内置工具
    干活，但**不会静默地少几个工具**。
    """
    from fa.mcp import ledger_client
    from fa.mcp.adapt import mount_tools

    client = ledger_client()
    tools = tuple(mount_tools(client, prefix=MCP_PREFIX))
    if tools:
        # 进程退出时把子进程收掉。不注册的话，退出时可能留下一个孤儿进程
        # —— 而它的 stdin 已经关了，它会一直等在 readline 上。
        atexit.register(client.close)
    return tools


def build_tools(
    confirm: Confirm | None = None,
    get_bill: Bill | None = None,
    *,
    use_mcp: bool = True,
) -> list[BaseTool]:
    """构造这个 Session 能用的全部工具。

    `confirm is None` 表示**全部放行**（`python -m fa --yes` 走的就是这条）。
    `use_mcp=False` 用来在不想要外部进程的场景下（比如某些测试）跳过挂载。
    """
    bill = get_bill if get_bill is not None else load_bill
    tools = [
        *build_query_tools(bill),
        *build_anomaly_tools(bill),
        *build_knowledge_tools(),
        *build_categorize_tools(confirm),
        *build_memory_tools(),
        *build_skill_tools(),
        *build_delegate_tools(bill),
    ]
    if use_mcp:
        tools.extend(_mcp_tools())
    return tools
