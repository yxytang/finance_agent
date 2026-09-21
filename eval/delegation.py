"""量一下子 agent 到底省了多少上下文。

    python -m eval.delegation

## 怎么量

「开子 agent」和「不开子 agent」跑同一个任务，比较主上下文增长了多少。

「不开」的那一次不好直接跑 —— 让模型「别用子 agent」只会得到一次不确定的
行为，而不可复现的对照等于没有对照。所以这里用一个**确定的基线**：

    子 agent 中间过程占的字符数 = 如果这些过程都留在主上下文里，会多出来的量

省下的 = 子 agent 上下文总字符 − 回传的字符。这个数字是从系统自己报的账里
读出来的，不是另外算一套 —— 两套算法会漂移，然后有一处忘了改。

## 为什么值得单独做个脚本

「子 agent 省上下文」是个很容易说出口、但很少被真的量过的结论。不量的话
它永远是个说法；量了之后有可能是负的（中间过程少而结论长时就会是负的），
**而那种情况你会想早点知道**。
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_core.messages import ToolMessage  # noqa: E402

from fa.agent import Session  # noqa: E402

TASK = "帮我分析一下我的消费，看看哪里可以省"

# 和 Delegation.report() 的格式对应。改那边就要改这里 —— 所以下面有一道
# 「一次委派都没解析到就报错」的检查，免得格式改了之后这里静默地量出 0。
_NUMBERS = re.compile(r"用了 (\d+) 步、([\d,]+) 字符的上下文，回传 ([\d,]+) 字符")


def _to_int(text: str) -> int:
    return int(text.replace(",", ""))


def _chars(messages) -> int:
    total = 0
    for message in messages:
        content = getattr(message, "content", "")
        total += len(content) if isinstance(content, str) else len(str(content))
    return total


def main() -> int:
    session = Session(on_event=None)

    print(f"任务：{TASK}\n")
    answer = session.send(TASK)

    reports = [
        found
        for m in session.messages
        if isinstance(m, ToolMessage)
        if (found := _NUMBERS.search(m.content))
    ]

    investigate_calls = sum(
        1
        for m in session.messages
        for call in (getattr(m, "tool_calls", None) or [])
        if call.get("name") == "investigate"
    )

    if investigate_calls and not reports:
        print(
            f"⚠️ 调了 {investigate_calls} 次 investigate，却一行账都没解析出来。\n"
            "多半是 Delegation.report() 的文案变了而这里的正则没跟着改。"
        )
        return 1

    sub_chars = sum(_to_int(r.group(2)) for r in reports)
    back_chars = sum(_to_int(r.group(3)) for r in reports)
    main_chars = _chars(session.messages)

    print(f"主 agent 调了 {investigate_calls} 次子助手。\n")
    print(f"  主上下文总计            {main_chars:>9,} 字符")
    if reports:
        print(f"  子助手内部过程合计      {sub_chars:>9,} 字符   ← 这些**没有**进主上下文")
        print(f"  回传给主 agent 的       {back_chars:>9,} 字符")
        print(f"  ─────────────────────────────────")
        print(f"  省下                    {sub_chars - back_chars:>9,} 字符")
        if not sub_chars:
            print("\n（省下为 0 说明一次子任务都没产生中间过程，多半没真跑起来。）")

    print(f"\n{'─' * 50}\n{answer}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
