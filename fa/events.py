"""事件流：agent 跑一轮的过程里往外播报的离散节点。

为什么需要它：一轮 `send()` 要跑几十秒，中间有多次工具往返。传统那种
「进一个字符串、出一个字符串」的接口把这些中间过程全丢了 —— 终端里还能
靠 print 糊过去，浏览器里就只能是转圈等到死。**过程本身就是内容**，所以
得把它做成一个正式的、可替换的接口，而不是散落在循环里的 print。

为什么是回调，而不是别的：

  - **生成器**会把接口裂成两种形状（有时返回字符串、有时返回生成器），
    调用方得先判断自己拿到的是哪种，而且答案和过程还得从两个地方取；
  - **全局队列**在同一个进程里跑两个会话时会串台 —— 第八天的多会话正好
    就是这个场景。

为什么事件是**朴素 dict**：它要原样 `json.dumps` 塞进 WebSocket。套一层
dataclass 只多出一次 asdict 转换，换不来任何东西。

一条硬约定：**监听器抛异常不能影响 agent 循环。** 事件是在工具循环内部播
的（见 `agent._run_tool_calls`），监听器一旦从那里抛出去，后面剩下的
tool_call 就没人应答，正好踩坏「每个 tool_call 恰好一条 ToolMessage」那条
不变量 —— 一个观察者把被观察者搞坏了。所以 `_emit` 一律吞掉。
"""

from collections.abc import Callable

Listener = Callable[[dict], None]

STEP_START = "step_start"
TOOL_CALL = "tool_call"
TOOL_RESULT = "tool_result"
ANSWER = "answer"
ERROR = "error"


def console_listener() -> Listener:
    """终端渲染。CLI 只是事件流的消费者之一，和第八天的 WebSocket 平级。

    工具那段输出和 Day 1 完全一致（`→` 发起、`←` 返回），所以换到事件驱动
    之后，肉眼看不出任何变化 —— 这正是「事件序列没丢信息」的证据。

    `error` 和 `answer` 分开渲染，是因为它们语义不同：前者是**流程自己**
    失败了、模型一个字都没说，后者才是模型真的答了。混在一起展示，用户会
    把「上下文超限了」当成 agent 的观点。
    """

    def listen(event: dict) -> None:
        kind = event["type"]

        if kind == TOOL_CALL:
            print(f"  → {event['name']}({event['args']})")
        elif kind == TOOL_RESULT:
            content = event["content"]
            first = content.splitlines()[0][:160] if content else "(空)"
            print(f"  ← {event['name']}({event['args']})\n    {first}")
        elif kind == ANSWER:
            print(f"\n{event['text']}\n")
        elif kind == ERROR:
            print(f"\n{event['message']}\n")
        # `step_start` 没有分支，是**故意**不显示的：终端里连着几轮工具调用
        # 本来就分不清步数，多打一行「第 3 步」只是噪音。但第八天的浏览器
        # 需要它 —— 那才是它存在的理由。别的消费者读事件时别以为漏了。

    return listen
