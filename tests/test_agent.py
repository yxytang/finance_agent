"""Session 循环 —— 从 forge 搬过来的那部分。

这些测试的作用是**证明搬迁没有搬坏**。骨架和领域无关，所以断言基本原样
保留：悬空 tool_call 的保证、指纹门控、事件流。改它们之前先想清楚是不是
把一条真正的设计约束给删了。

刻意不打真 API：这些测试要能在 CI 里免费跑（不需要 key）。
"""

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from fa.agent import Session, _is_content_filtered, _is_context_overflow
from fa.events import (
    ANSWER,
    ERROR,
    STEP_START,
    STOPPED,
    TOOL_CALL,
    TOOL_RESULT,
    console_listener,
)


class _Raiser:
    """一个调用就抛的工具桩。"""

    def __init__(self, exc):
        self.exc = exc

    def invoke(self, args):
        raise self.exc


class _ScriptedModel:
    """按剧本依次吐出回复的假模型。

    直接塞给 `session._model`，绕过 `build_model()` —— 这些测试不能联网，
    也就不能在 CI 上要 key。
    """

    def __init__(self, *replies):
        self.replies = list(replies)

    def invoke(self, messages):
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


def session(**kwargs) -> Session:
    return Session(confirm=None, **kwargs)


# --- 错误分类 -----------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "This model's maximum context length is 65536 tokens",
        "context_length_exceeded",
        "Please reduce the length of the messages",
        "too many tokens",
    ],
)
def test_recognizes_context_overflow(text):
    assert _is_context_overflow(RuntimeError(text))


def test_does_not_misread_other_errors():
    assert not _is_context_overflow(RuntimeError("401 Unauthorized"))


def test_detects_content_filter():
    """DeepSeek 的内容过滤是概率性的，会返回空回复。

    不认出来的话，它和「模型就是没说话」长得一模一样，很容易被当成程序 bug
    去查检索链路 —— 而问题其实在服务端策略上，本地怎么改都没用。
    """
    msg = SimpleNamespace(response_metadata={"finish_reason": "content_filter"})
    assert _is_content_filtered(msg)


def test_normal_stop_is_not_content_filter():
    msg = SimpleNamespace(response_metadata={"finish_reason": "stop"})
    assert not _is_content_filtered(msg)


# --- 悬空 tool_call 的不变量 --------------------------------------------


def test_every_tool_call_gets_exactly_one_tool_message():
    """整个 agent 最重要的一条不变量。

    只要有一个 tool_call 没拿到 ToolMessage，下一轮 invoke 就会被 API 拒绝：
    "assistant message with tool_calls must be followed by tool messages"。
    所以工具炸了、工具名写错了，都必须补一条消息。
    """
    s = session()
    s.tool_map["boom"] = _Raiser(RuntimeError("炸了"))

    s._run_tool_calls(SimpleNamespace(tool_calls=[
        {"name": "boom", "args": {}, "id": "1"},
        {"name": "根本不存在的工具", "args": {}, "id": "2"},
    ]))

    messages = [m for m in s.messages if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in messages] == ["1", "2"]
    assert "工具执行失败" in messages[0].content
    assert "不存在名为" in messages[1].content


def test_keyboard_interrupt_still_fills_every_tool_message():
    """Ctrl-C 也不能留下悬空的 tool_call —— 先补齐，再把中断往外抛。"""
    s = session()
    s.tool_map["boom"] = _Raiser(KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        s._run_tool_calls(SimpleNamespace(tool_calls=[
            {"name": "boom", "args": {}, "id": "1"},
            {"name": "nope", "args": {}, "id": "2"},
        ]))

    messages = [m for m in s.messages if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in messages] == ["1", "2"]


# --- system prompt 的指纹门控 -------------------------------------------


def test_system_prompt_is_not_replaced_when_unchanged():
    """门控保护的是 DeepSeek 的前缀缓存（命中大约便宜 10 倍）。

    每轮都动 `messages[0]` 的话，整段对话的缓存全失效。
    """
    s = session()

    assert s.refresh_system_prompt() is True  # 第一次：插入
    first = s.messages[0]
    assert s.refresh_system_prompt() is False  # 内容没变
    assert s.messages[0] is first  # 同一个对象，没被替换


def test_a_new_skill_trips_the_fingerprint_gate(make_skill):
    """skill 清单是 system prompt 里唯一每轮重算的输入。

    它没变就不该动 `messages[0]`；变了才替换。
    """
    s = session()
    s.refresh_system_prompt()
    first = s.messages[0]

    assert s.refresh_system_prompt() is False

    make_skill("monthly-report", "---\nname: monthly-report\ndescription: 月度报告\n---\n正文\n")

    assert s.refresh_system_prompt() is True
    assert s.messages[0] is not first
    assert "monthly-report" in s.messages[0].content


def test_reset_clears_history_and_fingerprint():
    s = session()
    s.refresh_system_prompt()

    s.reset()

    assert s.messages == []
    assert s.refresh_system_prompt() is True


# --- 事件流 -------------------------------------------------------------


def test_event_stream_reconstructs_the_turn():
    """光看事件序列就能还原这一轮干了什么。

    不传 `on_event` 时这个测试根本跑不起来 —— 事件漏播一步就会被逮住，
    而这正是「过程本身是内容」的前提。
    """
    events: list[dict] = []
    s = session(on_event=events.append)
    s.tool_map["lookup"] = SimpleNamespace(invoke=lambda args: "     1\t42.10")
    s._model = _ScriptedModel(
        AIMessage(content="", tool_calls=[{"name": "lookup", "args": {"q": "x"}, "id": "c1"}]),
        AIMessage(content="上个月外卖 42.10"),
    )

    assert s.send("上个月外卖花了多少") == "上个月外卖 42.10"

    assert [e["type"] for e in events] == [
        STEP_START, TOOL_CALL, TOOL_RESULT, STEP_START, ANSWER,
    ]
    assert events[0]["step"] == 1 and events[3]["step"] == 2

    call, result = events[1], events[2]
    assert call["name"] == "lookup" and call["args"] == {"q": "x"}
    assert result["call_id"] == "c1"
    assert result["ok"] is True
    assert "42.10" in result["content"]
    assert events[4]["text"] == "上个月外卖 42.10"


def test_failed_tool_call_is_reported_as_not_ok():
    """工具炸了在事件里有明确标记 —— UI 要能把它染红，而不是猜字符串。"""
    events: list[dict] = []
    s = session(on_event=events.append)
    s.tool_map["boom"] = _Raiser(RuntimeError("炸了"))
    s._model = _ScriptedModel(
        AIMessage(content="", tool_calls=[{"name": "boom", "args": {}, "id": "c1"}]),
        AIMessage(content="算了"),
    )

    s.send("随便")

    result = next(e for e in events if e["type"] == TOOL_RESULT)
    assert result["ok"] is False
    assert "工具执行失败" in result["content"]


def test_failure_paths_emit_error_not_answer():
    """流程自己失败（上下文超限、模型报错）走的是 error 事件。

    和 answer 分开不是审美问题：这是**模型一个字都没说**。混在一起展示，
    用户会把「上下文超限了」当成 agent 的结论。
    """
    events: list[dict] = []
    s = session(on_event=events.append)
    s._model = _ScriptedModel(
        RuntimeError("This model's maximum context length is 65536 tokens")
    )

    text = s.send("你好")

    assert [e["type"] for e in events] == [STEP_START, ERROR]
    assert "上下文超限" in text
    assert events[1]["message"] == text
    # 用户那句话被回滚了，会话还能接着用。检查点是插完 system prompt 才取的，
    # 所以剩下的那条 SystemMessage 是正常的，不该被当成残留。
    assert not [m for m in s.messages if isinstance(m, HumanMessage)]


def test_listener_exception_cannot_break_the_tool_loop(capsys):
    """监听器炸了不能留下悬空的 tool_call。

    事件是在工具循环内部播的，监听器从这里抛出去，同一轮后面剩下的
    tool_call 就没人应答 —— 观察者把被观察者弄坏了。所以 `_emit` 一律吞。
    """

    def boom(event):
        raise RuntimeError("队列满了")

    s = session(on_event=boom)
    s.tool_map["boom"] = _Raiser(RuntimeError("炸了"))
    s._model = _ScriptedModel(
        AIMessage(content="", tool_calls=[
            {"name": "boom", "args": {}, "id": "c1"},
            {"name": "nope", "args": {}, "id": "c2"},
        ]),
        AIMessage(content="算了"),
    )

    s.send("随便")

    tool_messages = [m for m in s.messages if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in tool_messages] == ["c1", "c2"]
    assert s._on_event is None  # 摘掉了

    err = capsys.readouterr().err
    assert err.count("事件监听器抛了") == 1  # 只喊一次，不刷屏


def test_no_listener_is_fine():
    """不传监听器就是「只关心答案」—— 评测脚本和测试都这么用。"""
    s = session()
    s._model = _ScriptedModel(AIMessage(content="就这样"))
    assert s.send("你好") == "就这样"


def test_console_listener_renders_tool_calls(capsys):
    listen = console_listener()
    listen({"type": TOOL_CALL, "call_id": "c1", "name": "query_transactions",
            "args": {"agg": "sum"}})
    listen({"type": TOOL_RESULT, "call_id": "c1", "name": "query_transactions",
            "args": {"agg": "sum"}, "content": "合计 1,234.56\n按类目分组…",
            "ok": True})
    listen({"type": ANSWER, "text": "上个月外卖 1,234.56"})

    out = capsys.readouterr().out
    assert "→ query_transactions" in out
    assert "← query_transactions" in out
    assert "合计 1,234.56" in out  # 只取第一行
    assert "上个月外卖 1,234.56" in out


# --- 打断一轮 -----------------------------------------------------------


def test_stopping_mid_tool_calls_still_answers_every_call():
    """停指令到达时，**每条 tool_call 仍然要有一条 ToolMessage**。

    这是全文件最要紧的不变量（见 `_run_tool_calls` 的说明）：少一条，下一轮
    invoke 就会被 API 直接拒绝 —— 而用户停完马上就会接着问下一句。

    所以停止**不能**从工具循环里直接抛出去，必须先把剩下的结果补齐。
    """
    s = session()
    ran: list[dict] = []

    def stop_then_run(args):
        s.stop()  # 第一条工具一跑就按停
        ran.append(args)
        return "结果"

    s.tool_map["lookup"] = SimpleNamespace(invoke=stop_then_run)
    s._model = _ScriptedModel(
        AIMessage(
            content="",
            tool_calls=[
                {"name": "lookup", "args": {"q": "1"}, "id": "c1"},
                {"name": "lookup", "args": {"q": "2"}, "id": "c2"},
            ],
        ),
    )

    answer = s.send("跑两个")

    assert ran == [{"q": "1"}]  # 第二条没执行
    answered = [m for m in s.messages if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in answered] == ["c1", "c2"]  # 但两条都有应答
    assert "已停止" in answered[1].content
    assert "已停止" in answer


def test_stop_after_the_last_tool_call_still_stops():
    """信号在**最后一个**工具执行期间到达时，靠的是步与步之间那次检查。

    每个工具调用之前查一次还不够 —— 最后一次执行期间到达的信号，那时候已经没有
    「下一个工具」可以查了。没有那一步之间的检查，就会白跑一次模型调用。
    """
    s = session()
    s.tool_map["lookup"] = SimpleNamespace(invoke=lambda args: (s.stop(), "结果")[1])
    model = _ScriptedModel(
        AIMessage(content="", tool_calls=[{"name": "lookup", "args": {}, "id": "c1"}]),
        AIMessage(content="不该被调到"),
    )
    s._model = model

    answer = s.send("跑一个")

    assert "已停止" in answer
    assert len(model.replies) == 1  # 第二次模型调用没有发生


def test_a_stale_stop_does_not_kill_the_next_turn():
    """停完接着问是正常操作 —— 上一轮留下的信号必须被清掉。

    不清的话新一轮会在第一步就被立刻停掉，而那个表现看起来像「它坏了」。
    """
    s = session()
    s._model = _ScriptedModel(AIMessage(content="正常回答"))
    s.stop()

    assert s.send("接着问") == "正常回答"


def test_stop_emits_stopped_not_error_and_not_answer():
    """停止是第三种语义 —— 既不是故障也不是模型的回答。

    混进 error，用户会以为坏了；混进 answer，用户会把那句「已停止」当成 agent
    的观点。
    """
    events: list[dict] = []
    s = session(on_event=events.append)
    s.tool_map["lookup"] = SimpleNamespace(invoke=lambda args: (s.stop(), "结果")[1])
    s._model = _ScriptedModel(
        AIMessage(content="", tool_calls=[{"name": "lookup", "args": {}, "id": "c1"}]),
        AIMessage(content="不该被调到"),
    )

    s.send("跑一个")

    kinds = [e["type"] for e in events]
    assert STOPPED in kinds
    assert ERROR not in kinds
    assert ANSWER not in kinds
    assert "已停止" in next(e for e in events if e["type"] == STOPPED)["text"]
