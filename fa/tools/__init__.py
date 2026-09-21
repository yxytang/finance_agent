"""工具集。

`build_tools()` 是唯一入口：条件挂载全在这里，agent 主体不用知道某个工具
存不存在。第七天往这里塞 MCP 动态构造出来的工具 —— 到时候 agent.py 一行都不用
改，这就是这层存在的意义。

Day 1 只有 `use_skill` 一个：查询、异常、检索的工具分别在 Day 3 / Day 3 / Day 5
接进来。
"""

from langchain_core.tools import BaseTool

from fa.permissions import Confirm
from fa.tools.skills import build_skill_tools


def build_tools(confirm: Confirm | None = None) -> list[BaseTool]:
    """构造这个 Session 能用的全部工具。

    `confirm` 现在还没人用（第一个会写数据的工具在第四天）。参数先留着，
    免得那时候要改所有调用点。
    """
    return [
        *build_skill_tools(),
    ]
