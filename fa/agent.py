"""Agent 循环 + 多轮会话状态。

循环形状是 `bind_tools → invoke → 有 tool_calls 就执行`。多出来的几件事
都是 REPL 场景才需要操心的：

  1. **悬空 tool_call 的保证**（见 _run_tool_calls）—— 单次问答时循环天然
     跑完，在 REPL 里却可能被 Ctrl-C 打断，留下没被应答的 tool_call，
     下一轮 invoke 会被 API 直接拒绝。
  2. **system message 按指纹替换** —— 保住前缀缓存。
  3. **上下文超限的可读兜底** —— 而不是抛一串 traceback 给用户。

这个模块**不打印任何东西**。过程通过 `on_event` 往外播报（见 events.py），
谁来渲染是调用方的事 —— 终端和浏览器是平级的两个消费者。真的往这里塞
print，就等于把「浏览器里什么都不显示」这个 bug 焊死在循环里。
"""

import sys

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from fa.config import MAX_STEPS, build_model
from fa.events import ANSWER, ERROR, STEP_START, TOOL_CALL, TOOL_RESULT
from fa.prompt import build_system_prompt
from fa.skills import discover
from fa.tools import build_tools

_CONTEXT_OVERFLOW_MARKERS = (
    "context length",
    "maximum context",
    "too many tokens",
    "context_length_exceeded",
    "reduce the length",
)


def _is_context_overflow(exc: Exception) -> bool:
    """各家的上下文超限错误文案不统一，只能按关键字认。

    认错了也不要紧：最坏情况是多报一次「上下文满了」，用户重开一段就是。
    """
    text = str(exc).lower()
    return any(marker in text for marker in _CONTEXT_OVERFLOW_MARKERS)


def _is_content_filtered(ai_message) -> bool:
    """这次回复是不是被服务端内容过滤掐掉的。

    被拦时 API 返回 `finish_reason="content_filter"` 且 **completion_tokens=0**
    —— 一个 token 都没产出，content 是空串、也没有 tool_calls。所以在调用方
    看来它和「模型就是没说话」长得一模一样，很容易被当成程序 bug 去查检索
    链路，而问题其实在服务端策略上，本地怎么改都没用。

    这个过滤是概率性的：同一句话重问，有时正常返回、有时被拦。
    """
    metadata = getattr(ai_message, "response_metadata", None) or {}
    return metadata.get("finish_reason") == "content_filter"


class Session:
    """一次对话。messages 跨轮持久，这就是「多轮」的全部含义。

    `on_event` 是可选的过程监听器，收到的是 events.py 里那几种 dict。
    不传就是「只关心最终答案」—— 测试和第七天的评测都这样用。
    """

    def __init__(self, confirm=None, on_event=None):
        self.messages: list = []
        self._on_event = on_event
        self._model = None
        self._prompt_fingerprint: str | None = None

        self.tools = build_tools(confirm)
        self.tool_map = {t.name: t for t in self.tools}

    # ------------------------------------------------------------------
    # 事件播报
    # ------------------------------------------------------------------

    def _emit(self, event: dict) -> None:
        """播报一个事件。

        监听器抛异常**必须吞掉**。这不是假想问题：`tool_result` 是在工具
        循环内部播的，监听器（比如往一个已经满了的队列里塞）一旦从那里抛
        出去，同一轮后面剩下的 tool_call 就没人应答，直接踩坏本文件最要紧
        的那条不变量。观察者不该有能力弄坏被观察者。

        吞掉之后要做两件事：喊一声（否则静默失败，UI 再也不更新了却没人
        知道为什么），然后把自己摘掉（不然每一步都喊一遍，把真正的报错
        冲没了）。
        """
        if self._on_event is None:
            return
        try:
            self._on_event(event)
        except Exception as exc:  # noqa: BLE001 - 见上：绝不能往外抛
            self._on_event = None
            print(
                f"警告：事件监听器抛了 {type(exc).__name__}: {exc}，已停用。",
                file=sys.stderr,
            )

    def _fail(self, message: str) -> str:
        """播报一条错误事件，并把同一段文本当作本轮答复返回。"""
        self._emit({"type": ERROR, "message": message})
        return message

    # ------------------------------------------------------------------
    # 模型
    # ------------------------------------------------------------------

    def _get_model(self):
        """懒构造。key 没配好时，错误只在真正要用的时候才暴露。"""
        if self._model is None:
            self._model = build_model().bind_tools(self.tools)
        return self._model

    # ------------------------------------------------------------------
    # system prompt
    # ------------------------------------------------------------------

    def refresh_system_prompt(self) -> bool:
        """重渲染 system prompt，返回是否真的变了。

        只有渲染结果变化时才替换 `messages[0]`：DeepSeek 按精确 token 前缀
        命中缓存，每轮都动 system message 会让整段对话的缓存失效。

        真正会变的输入只有 skill 清单（每轮重扫，所以 agent 刚写完的
        SKILL.md 下一轮自己就能用上）。清单没变就一次替换都不做。
        """
        rendered = build_system_prompt(discover())
        if rendered == self._prompt_fingerprint:
            return False

        self._prompt_fingerprint = rendered
        if self.messages and isinstance(self.messages[0], SystemMessage):
            self.messages[0] = SystemMessage(rendered)
        else:
            self.messages.insert(0, SystemMessage(rendered))
        return True

    # ------------------------------------------------------------------
    # 工具执行
    # ------------------------------------------------------------------

    def _execute(self, call: dict) -> str:
        """执行单个工具调用。工具自身已经把预期内的错误转成字符串了，
        这里只管把工具名写错、或工具内部真出 bug 的情况兜住。"""
        name = call.get("name", "")
        fn = self.tool_map.get(name)
        if fn is None:
            return (
                f"错误：不存在名为 {name} 的工具。"
                f"可用工具：{', '.join(self.tool_map)}"
            )
        return str(fn.invoke(call.get("args") or {}))

    def _run_tool_calls(self, ai_message) -> None:
        """执行本轮所有工具调用，并把结果追加进 messages。

        **这是整个文件最关键的一段。** 必须保证每一个 tool_call 都恰好拿到
        一条 ToolMessage —— 只要有一条没被应答，下一轮 invoke 就会被 API 拒绝：
        "assistant message with tool_calls must be followed by tool messages"。

        所以这里是「先全部补齐，再决定要不要抛」，连 Ctrl-C 也不例外。
        """
        interrupted: BaseException | None = None

        for call in ai_message.tool_calls:
            ok = True
            try:
                content = self._execute(call)
            except KeyboardInterrupt as exc:
                interrupted = exc
                ok = False
                content = "已中断：用户取消了这次工具调用。"
            except Exception as exc:  # noqa: BLE001 - 全部转成可读的观察结果
                ok = False
                content = f"工具执行失败：{type(exc).__name__}: {exc}"

            self.messages.append(ToolMessage(content=content, tool_call_id=call["id"]))
            self._emit(
                {
                    "type": TOOL_RESULT,
                    "call_id": call["id"],
                    # 名字和参数**故意重复播一遍**：这样每条事件都是自足的，
                    # 消费者不必拿 call_id 去和前面那条 tool_call 配对。
                    # 让 UI 自己维护配对表，是 bug 的温床。
                    "name": call["name"],
                    "args": call.get("args"),
                    "content": content,
                    "ok": ok,
                }
            )

        # 补齐之后才把中断往外抛，历史保持合法。
        if interrupted is not None:
            raise interrupted

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def send(self, text: str) -> str:
        """发一轮消息，跑完工具循环，返回最终答复文本。

        中间过程通过 `on_event` 播报出去，但**返回值依然是纯字符串** ——
        程序化调用方（测试、第七天的评测）不需要理会事件，也不需要为了拿到
        答案而在回调里做状态机。两条通道各管各的。
        """
        self.refresh_system_prompt()

        # 记下检查点：上下文超限时回滚到这里，让会话仍然可用。
        checkpoint = len(self.messages)
        self.messages.append(HumanMessage(text))

        for step in range(1, MAX_STEPS + 1):
            self._emit({"type": STEP_START, "step": step})

            try:
                ai_message = self._get_model().invoke(self.messages)
            except Exception as exc:  # noqa: BLE001
                del self.messages[checkpoint:]
                if _is_context_overflow(exc):
                    return self._fail(
                        "上下文超限了 —— 这段对话加上工具结果已经塞不进模型窗口。\n"
                        "这一轮没有留下痕迹，你可以：\n"
                        "  · 输入 /reset 开一段新对话\n"
                        "  · 或者让它只读文件的一部分（read_file 有 offset/limit）"
                    )
                return self._fail(f"调用模型失败：{type(exc).__name__}: {exc}")

            self.messages.append(ai_message)

            if not ai_message.tool_calls:
                answer = (ai_message.content or "").strip()
                if answer:
                    self._emit({"type": ANSWER, "text": answer})
                    return answer
                if _is_content_filtered(ai_message):
                    return self._fail(
                        "这次回复被 DeepSeek 的**内容过滤**拦掉了：API 返回了 "
                        "`finish_reason=content_filter`，completion_tokens 为 0，"
                        "一个字都没生成。重问一次往往就正常了。"
                    )
                return self._fail("(模型没有返回内容)")

            for call in ai_message.tool_calls:
                self._emit(
                    {
                        "type": TOOL_CALL,
                        "call_id": call["id"],
                        "name": call["name"],
                        "args": call.get("args"),
                    }
                )

            self._run_tool_calls(ai_message)

        # 步数用完。此刻每条 tool_call 都已经有应答，历史是合法的 ——
        # 不需要（也不该）伪造一条 assistant 消息，直接告诉用户就行。
        return self._fail(
            f"已经连续做了 {MAX_STEPS} 步还没有收敛，先停在这里。\n"
            f"把任务拆小一点，或者直接告诉我下一步该看什么。"
        )

    def reset(self) -> None:
        self.messages.clear()
        self._prompt_fingerprint = None
