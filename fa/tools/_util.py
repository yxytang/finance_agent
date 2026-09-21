"""工具层共用的小东西。"""

from fa.config import MAX_TOOL_OUTPUT


def truncate(text: str) -> str:
    """超长就截断，并且**留话说明截掉了多少**。

    留这句很重要：模型看到结果断在半句上，会以为那就是全部，
    然后基于一个残缺的观察继续下结论。
    """
    if len(text) <= MAX_TOOL_OUTPUT:
        return text
    return text[:MAX_TOOL_OUTPUT] + f"\n…[已截断，原文共 {len(text)} 字符]"
