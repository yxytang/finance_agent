"""跨会话记忆的工具。

只有一个 `remember`，因为**记什么由模型判断，能记什么由 extract 把关** ——
分成两个工具（「记偏好」「记事实」）只会让模型多一个选错的机会，而类型本来
也不影响记忆怎么用。
"""

from datetime import date

from langchain_core.tools import BaseTool, tool

from fa.memory import Memory, add, load
from fa.memory.extract import guess_kind, judge


def _memory_file():
    """从 config **在调用时**读。测试要把记忆换到临时目录，写死就换不动了。"""
    from fa import config

    return config.MEMORY_FILE


def build_memory_tools() -> list[BaseTool]:
    @tool
    def remember(text: str) -> str:
        """把一条**会长期有用**的信息记下来，跨会话保留。

        什么时候用：
        - 用户明确说「记住…」「以后…」「下次别…」
        - 用户陈述了自己的长期情况（住哪、习惯、不用的东西）
        - 你发现了一个**以后还会再遇到**的约定

        **什么时候别用**：一次性的任务细节。「这次看看 3 月」记下来，下次用户问
        4 月时它就是错的，而且看起来还挺合理 —— 这类记忆比没有记忆更糟。

        记下来的内容会进 system prompt，所以写**短而完整的一句**：
        写「房租归住房类」而不是「用户今天跟我提到他希望把房租这个支出归到住房
        这个分类里面去」。记忆多了会稀释彼此的权重，写得越长稀释越厉害。
        """
        ok, reason = judge(text)
        if not ok:
            return f"没记：{reason}"

        memory = Memory(text=text.strip(), kind=guess_kind(text), created=date.today())
        path = _memory_file()

        if any(m.text == memory.text for m in load(path)):
            return f"这条已经记过了：{memory.text}"

        memories = add(path, memory)
        return (
            f"已记下（{memory.kind}）：{memory.text}\n"
            f"当前共 {len(memories)} 条记忆。用 /memory 可以查看和删除。"
        )

    return [remember]
