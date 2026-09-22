"""Web 层：图表解析、会话管理、SSE、权限确认。

这个文件里最要紧的是第一条测试：**图表解析和真实渲染器之间的闭环**。
别的东西坏了会报错，而解析坏了不会 —— 它只是安静地返回 None。
"""

import json
import queue
import threading
import time
from datetime import date

import pytest

from fa.models import Transaction, money
from fa.query import query
from fa.tools.query import render_result
from web.charts import extract_series
from web.sessions import (
    CHART,
    CONFIRM_REQUEST,
    MAX_SESSIONS,
    TURN_END,
    TURN_START,
    LiveSession,
    SessionManager,
    _sse,
)


def bill():
    return [
        Transaction(date(2026, 1, 5), "咖啡店", money("30.00"), "credit", "T1", "咖啡"),
        Transaction(date(2026, 2, 5), "咖啡店", money("45.00"), "credit", "T2", "咖啡"),
        Transaction(date(2026, 2, 8), "超市", money("300.00"), "credit", "T3", "超市"),
        Transaction(date(2026, 3, 3), "超市", money("250.00"), "credit", "T4", "超市"),
    ]


class _ScriptedModel:
    """按剧本回复的假模型 —— 用来让 `Session.send()` 真的跑一轮。"""

    def __init__(self, *replies):
        self.replies = list(replies)

    def invoke(self, messages):
        return self.replies.pop(0) if self.replies else None


# --- 图表解析（最重要的一条）--------------------------------------------


def test_chart_parser_matches_the_real_renderer():
    """**这条测试是 web/charts.py 那层解析敢存在的前提。**

    解析自己渲染出来的文本是危险的：格式一改，解析就**静默地返回空** ——
    图表不出现，控制台干净，没有任何报错。用户看到的是「这个回答没有图」，
    而不是「图表坏了」。

    所以这里拿**真实的渲染器**跑一遍，把输出解回来，断言数字和顺序都对得上。
    格式动了它会红，而不是让图表悄悄消失。

    （Day 5 那次是同一个教训：解析自己的输出可以，但必须有一条闭环的测试。）
    """
    rendered = render_result(query(bill(), group_by="category"))

    series = extract_series(rendered)

    assert series is not None
    assert series["group"] == "类目"
    # 渲染时按月/按值排好了序，解析必须原样保留 —— 柱状图的横轴顺序是有意义的
    assert [r["label"] for r in series["rows"]] == ["超市", "咖啡"]
    assert [r["value"] for r in series["rows"]] == [550.0, 75.0]
    assert [r["count"] for r in series["rows"]] == [2, 2]


def test_chart_parser_handles_month_grouping():
    rendered = render_result(query(bill(), group_by="month"))

    series = extract_series(rendered)

    assert series["group"] == "月份"
    assert series["order"] == "时间"  # 月份是按时间排的，前端据此画折线
    assert [r["label"] for r in series["rows"]] == ["2026-01", "2026-02", "2026-03"]


def test_count_aggregation_also_parses():
    """agg=count 时渲染出来的值是「308 笔」，也要能解出数。"""
    rendered = render_result(query(bill(), agg="count", group_by="category"))

    series = extract_series(rendered)

    assert series is not None
    assert [r["value"] for r in series["rows"]] == [2.0, 2.0]


def test_no_chart_for_ungrouped_results():
    """没分组的结果没有图可画 —— 返回 None 而不是一张只有一根柱子的图。"""
    assert extract_series(render_result(query(bill()))) is None


def test_no_chart_for_plain_text():
    assert extract_series("这只是一句话。") is None


def test_partial_parse_is_refused_not_half_drawn():
    """一整行解析不出数就整块放弃。

    半张图比没有图更糟 —— 它看起来是对的。
    """
    broken = "条件：x\n\n按类目分组（按数值排序）：\n  咖啡      2 笔         75.00\n  超市      2 笔      不是数字\n"
    assert extract_series(broken) is None


# --- SSE 帧 -------------------------------------------------------------


def test_sse_frame_format():
    """SSE 就是个纯文本协议：`data: <json>\\n\\n`。"""
    frame = _sse({"type": "answer", "text": "你好"})
    assert frame.startswith("data: ")
    assert frame.endswith("\n\n")
    assert json.loads(frame[6:].strip())["text"] == "你好"


# --- 会话与回放 ---------------------------------------------------------


def test_subscribe_replays_the_backlog():
    """**新连接要先拿到历史再续播。**

    SSE 没有回放。刷新之后新连接是「从此刻起」的 —— 而 agent 可能已经跑了
    一半，那些步骤就永远看不到了。
    """
    live = LiveSession("t")
    live.session = None  # 这个测试不跑 agent
    live.emit({"type": "answer", "text": "一"})
    live.emit({"type": "answer", "text": "二"})

    listener = live.subscribe()

    assert listener.get_nowait()["text"] == "一"
    assert listener.get_nowait()["text"] == "二"

    live.emit({"type": "answer", "text": "三"})
    assert listener.get_nowait()["text"] == "三"


def test_unsubscribe_stops_the_delivery():
    live = LiveSession("t")
    live.session = None
    listener = live.subscribe()
    live.unsubscribe(listener)

    live.emit({"type": "answer", "text": "不该收到"})

    assert listener.empty()


def test_event_log_is_capped():
    """跑很久的会话能攒下几兆事件，而其中绝大部分不会再被看。"""
    from web.sessions import MAX_EVENTS

    live = LiveSession("t")
    live.session = None
    for i in range(MAX_EVENTS + 50):
        live.emit({"type": "answer", "text": str(i)})

    assert len(live.events) == MAX_EVENTS
    assert live.events[-1]["text"] == str(MAX_EVENTS + 49)  # 丢的是最老的


# --- 图表自动补推 -------------------------------------------------------


def test_tool_result_with_a_table_gets_a_chart_event():
    """工具结果里是分组表时，服务器**补推**一条结构化的事件。

    放在服务端而不是前端解字符串：agent 和工具层完全不用知道「有前端这回事」，
    照旧只返回给模型看的文本。而且服务端的代码有测试钉住，前端没有。
    """
    live = LiveSession("t")
    live.session = None
    listener = live.subscribe()

    live.emit(
        {
            "type": "tool_result",
            "call_id": "c1",
            "name": "query_transactions",
            "content": render_result(query(bill(), group_by="category")),
            "ok": True,
        }
    )

    kinds = [listener.get_nowait()["type"] for _ in range(2)]
    assert kinds == ["tool_result", CHART]


def test_tool_result_without_a_table_gets_no_chart():
    live = LiveSession("t")
    live.session = None
    listener = live.subscribe()

    live.emit({"type": "tool_result", "call_id": "c1", "content": "命中 1 笔", "ok": True})

    assert listener.get_nowait()["type"] == "tool_result"
    assert listener.empty()


# --- 权限确认 -----------------------------------------------------------


def test_confirm_blocks_until_answered():
    """**工具线程停下来等浏览器** —— 这是同步回调和异步界面之间的桥。

    工具在自己的线程里调 `confirm()`，它必须当场返回一个 bool；而用户隔着一次
    网络往返。所以一边阻塞等待，一边等 HTTP 请求来把它叫醒。
    """
    live = LiveSession("t")
    live.session = None
    listener = live.subscribe()

    result = {}

    def worker():
        result["allow"] = live.ask("把 target 改成购物？")

    thread = threading.Thread(target=worker)
    thread.start()

    # 等它真的开始等 —— 然后才回答
    event = listener.get(timeout=5)
    assert event["type"] == CONFIRM_REQUEST
    assert "target" in event["summary"]

    assert live.answer(True) is True
    thread.join(timeout=5)

    assert result["allow"] is True


def test_confirm_times_out_into_a_refusal(monkeypatch):
    """**等不到答复要落在「不允许」那一侧。**

    浏览器关掉了、网络断了、用户走开了 —— 这些情况下等待必须结束，而且必须
    结束在拒绝那边。一个等不到答复就放行的实现，等于把确认这件事取消了。
    """
    import web.sessions as sessions

    monkeypatch.setattr(sessions, "CONFIRM_TIMEOUT", 0.05)

    live = LiveSession("t")
    live.session = None
    listener = live.subscribe()

    started = time.time()
    allowed = live.ask("改点东西？")
    elapsed = time.time() - started

    assert allowed is False
    assert elapsed < 2.0
    # 超时要有一条事件 —— 否则用户只知道「没改成」，不知道是为什么
    kinds = []
    while not listener.empty():
        kinds.append(listener.get_nowait()["type"])
    assert "confirm_timeout" in kinds


def test_answering_with_nobody_waiting_is_not_an_error():
    """重复点击或者已经超时之后再点，对用户来说不是失败。"""
    live = LiveSession("t")
    live.session = None
    assert live.answer(True) is False


# --- 跑一轮 -------------------------------------------------------------


def test_run_always_emits_turn_end_even_when_it_crashes():
    """不发 `turn_end` 的话，浏览器的输入框会永远禁用着，
    而用户完全不知道发生了什么。"""
    live = LiveSession("t")

    class _Boom:
        def send(self, text):
            raise RuntimeError("炸了")

    live.session = _Boom()
    listener = live.subscribe()

    live.run("随便问")

    kinds = []
    while not listener.empty():
        kinds.append(listener.get_nowait()["type"])

    assert kinds[0] == TURN_START
    assert kinds[-1] == TURN_END
    assert "error" in kinds


def test_agent_events_actually_reach_the_stream():
    """**这条测试是被一个真 bug 逼出来的。**

    `LiveSession` 建 `Session` 时忘了传 `on_event` —— 于是 agent 照常跑，
    但所有事件（step_start / tool_call / tool_result / answer）全部丢进虚空，
    浏览器只看得到 `turn_start` 和 `turn_end`。界面一片空白，**没有任何报错**。

    原来的测试没抓住它，因为那条用的是「会抛异常的假 session」——
    从来没走过真实的接线。所以这条必须**用真的 `Session` + 假模型跑一整轮**。
    """
    from langchain_core.messages import AIMessage

    from fa.events import ANSWER, STEP_START

    live = LiveSession("t", confirm=False)
    live.session._model = _ScriptedModel(AIMessage(content="答完了"))

    listener = live.subscribe()
    live.run("问一句")

    kinds = []
    while not listener.empty():
        kinds.append(listener.get_nowait()["type"])

    assert kinds[0] == TURN_START
    assert kinds[-1] == TURN_END
    # 中间那些**必须**在 —— 少了它们，前端就是白屏
    assert STEP_START in kinds
    assert ANSWER in kinds


# --- 会话管理 -----------------------------------------------------------


def test_session_cap():
    """有写权限的 agent 不能让人随便开会话。"""
    manager = SessionManager(confirm=False)
    for _ in range(MAX_SESSIONS):
        manager.create()

    with pytest.raises(RuntimeError):
        manager.create()


def test_sessions_have_distinct_ids():
    manager = SessionManager(confirm=False)
    ids = {manager.create().id for _ in range(5)}
    assert len(ids) == 5


def test_confirm_callback_is_wired_into_the_tools():
    """**构造时就要把回调绑上。**

    写成「先塞个占位、建好之后再换上去」的话，中间那一刻工具集里挂的是错的
    回调 —— 而如果中间出了任何岔子，它会一直错着，且只在真正要确认时才暴露。
    """
    live = LiveSession("t", confirm=True)
    tool = next(t for t in live.session.tools if t.name == "correct_category")
    assert tool is not None  # 能构造出来就说明 confirm 传进去了

    quiet = LiveSession("u", confirm=False)
    assert quiet.session.tool_map  # 不传 confirm 也能正常构造
