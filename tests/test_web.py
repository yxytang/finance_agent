"""Web 层：图表解析、会话管理、SSE、权限确认。

这个文件里最要紧的是第一条测试：**图表解析和真实渲染器之间的闭环**。
别的东西坏了会报错，而解析坏了不会 —— 它只是安静地返回 None。
"""

import json
import queue
import re
import shutil
import subprocess
import threading
import time
from datetime import date
from pathlib import Path

import pytest

from fa.models import Transaction, money
from fa.query import query
from fa.tools.forecast import build_forecast_tools
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


def test_observed_series_are_marked_observed():
    """观测序列要带上 kind。

    没有这个字段的话前端只能默认「是实测」，而预测图会悄悄混进来 —— 然后一张
    「这个月大概会花多少」的推算图，看起来就和实测的一模一样。
    """
    series = extract_series(render_result(query(bill(), group_by="category")))

    assert series["kind"] == "observed"


def test_chart_parser_reads_a_projection_as_projected():
    """预测表的闭环 —— 和上面那条同一个道理。

    预测的渲染格式一改，图表要么**静默消失**、要么**被当成实测数据**，两种都
    不报错。所以必须拿**真实的预测工具**跑一遍再解回来。

    预测行**没有笔数**：推算数不出笔数来，解析出来的 count 必须是 None，
    而不是随手填一个数 —— 填了就是在编。
    """
    partial = [
        Transaction(date(2026, 9, 1), "超市", money("100.00"), "credit", "T1", "超市"),
        Transaction(date(2026, 9, 10), "超市", money("100.00"), "credit", "T2", "超市"),
    ]
    tools = build_forecast_tools(lambda: partial, lambda: date(2026, 9, 11))
    tool = next(t for t in tools if t.name == "project_spending")

    rendered = tool.invoke({"month": "2026-09"})

    series = extract_series(rendered)

    assert series is not None
    assert series["kind"] == "projected"
    assert [r["label"] for r in series["rows"]] == ["已发生到 09-10", "全月推算（30 天）"]
    assert [r["value"] for r in series["rows"]] == [200.0, 600.0]  # 200 / 10 × 30
    assert [r["count"] for r in series["rows"]] == [None, None]


def test_a_projected_block_is_not_mistaken_for_an_observed_one():
    """两个表头必须互斥 —— 认错的话推算就会被画成实测。"""
    projected = "按时间范围分组（按数值排序，推算）：\n\n  甲  1.00\n  乙  2.00\n"

    series = extract_series(projected)

    assert series["kind"] == "projected"


def test_a_finished_month_is_not_a_projection():
    """月份已经过完、但账单缺了月末几天时，**不能**说成是预测。

    缺的是**过去**（那几天已经发生了，只是没记上），不是未来。说成「预计」会让
    用户以为在往前看，然后拿一个偏小的数当整月实际值。
    """
    gap = [
        Transaction(date(2026, 8, 1), "超市", money("100.00"), "credit", "T1", "超市"),
        Transaction(date(2026, 8, 20), "超市", money("100.00"), "credit", "T2", "超市"),
    ]
    tools = build_forecast_tools(lambda: gap, lambda: date(2026, 9, 11))
    tool = next(t for t in tools if t.name == "project_spending")

    rendered = tool.invoke({"month": "2026-08"})

    assert "已经过完了" in rendered
    assert "数据缺口" in rendered
    # 不出预测图 —— 这张图不是预测。
    assert extract_series(rendered) is None


def test_a_complete_month_reports_the_actual():
    """数据覆盖到月末时直接报实际值，标「预计」等于把准确的说成猜的。"""
    done = [
        Transaction(date(2026, 8, 1), "超市", money("100.00"), "credit", "T1", "超市"),
        Transaction(date(2026, 8, 31), "超市", money("200.00"), "credit", "T2", "超市"),
    ]
    tools = build_forecast_tools(lambda: done, lambda: date(2026, 9, 11))
    tool = next(t for t in tools if t.name == "project_spending")

    rendered = tool.invoke({"month": "2026-08"})

    assert "数据是完整的" in rendered
    assert "300.00" in rendered
    assert extract_series(rendered) is None


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


# --- 会话列表用的元数据 -------------------------------------------------
#
# 侧栏能不能用，全看这几个字段对不对。在此之前浏览器只能自己记一个 id，
# 刷新就丢 —— 服务端的回放一直在，却没有任何入口能走回去。


def test_summary_carries_what_the_sidebar_needs():
    live = LiveSession("abc123", confirm=False)

    item = live.summary()

    assert item["id"] == "abc123"
    assert item["turns"] == 0
    assert item["busy"] is False
    assert item["created_at"] == item["last_activity"]
    # 要能被浏览器的 new Date(...) 直接解。
    assert "T" in item["created_at"]


def test_summary_does_not_leak_the_conversation():
    """列表只要「有这么个会话」，不要内容 —— 内容靠连上去回放。

    这条是防手滑：`summary()` 里多塞一个正文/消息字段，就等于把会话内容发给了
    任何能列会话的人。
    """
    live = LiveSession("abc123", confirm=False)
    live.emit({"type": TURN_START, "text": "上个月外卖花了多少"})

    item = live.summary()

    assert set(item) == {"id", "created_at", "last_activity", "turns", "busy", "events"}
    assert "上个月外卖花了多少" not in str(item)


def test_summary_counts_turns():
    live = LiveSession("abc123", confirm=False)
    live.session.send = lambda text: None  # 不真跑 agent

    live.run("第一轮")
    live.run("第二轮")

    assert live.summary()["turns"] == 2


def test_manager_list_is_sorted_by_recent_activity():
    """最近动过的排前面 —— 用户要找的多半是刚才那个。"""
    manager = SessionManager(confirm=False)
    stale = manager.create()
    fresh = manager.create()

    # 直接把时间戳摆好，不靠 sleep 去赌时钟精度（Windows 上分辨率不保证）。
    stale.last_activity = fresh.last_activity - 60

    assert [item.id for item in manager.list()] == [fresh.id, stale.id]


def test_manager_list_includes_everything_it_created():
    manager = SessionManager(confirm=False)
    created = {manager.create().id for _ in range(3)}

    assert {item.id for item in manager.list()} == created


def test_stopping_an_idle_session_reports_nothing_running():
    """空闲时如实说「没在跑」，而不是假装接受了 —— 界面靠这个决定要不要收按钮。"""
    live = LiveSession("t", confirm=False)

    assert live.stop() is False


def test_stopping_a_busy_session_reaches_the_agent():
    """`LiveSession.stop()` 只是**转发** —— 只有 agent 那边知道怎么安全地停
    （模型调用拦不住，所以要停在当前这一步之后）。"""
    live = LiveSession("t", confirm=False)
    live.busy = True

    assert live.stop() is True
    assert live.session._stop.is_set()


# --- 前端渲染 -----------------------------------------------------------
#
# 前端在此之前完全没有测试。这条是第一个，它有具体的由来：`markdown()` 里刚发现
# 一个**不报错**的 bug —— 转义在匹配**之前**做，所以 `>` 已经是 `&gt;` 了，而
# 引用的正则还在匹配 `>`，永远匹配不上。表现是引用静默地退化成普通段落。
#
# 那种 bug 只有把函数真的跑起来才看得见，所以这里用 node 跑 —— 那段逻辑就是 JS。
# **没有 node 就跳过**（CI 上只装 Python，这条在那边不参与）。

_INDEX = Path(__file__).resolve().parent.parent / "web" / "index.html"


def _markdown_source() -> str:
    """从 index.html 里抠出 `markdown()` 的源码。"""
    html = _INDEX.read_text(encoding="utf-8")
    js = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)[-1]
    return js[js.index("function markdown(text)"): js.index("// 不是 newSession")]


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node，跑不了前端脚本")
def test_markdown_renders_lists_quotes_and_links():
    cases = [
        ["- 甲\n- 乙", "<ul>\n<li>甲</li>\n<li>乙</li>\n</ul>"],
        ["1. 甲\n2. 乙", "<ol>\n<li>甲</li>\n<li>乙</li>\n</ol>"],
        # 引用：标记里的 `>` 在匹配时已经是 `&gt;` 了 —— 这条就是那个 bug 的守卫
        ["> 一\n> 二", "<blockquote>\n<div>一</div>\n<div>二</div>\n</blockquote>"],
        ["**粗**和*斜*", "<div><strong>粗</strong>和<em>斜</em></div>"],
        # 表格不能被列表规则吃掉（`|---|---|` 里没有 `- `，但顺序变了就会出事）
        [
            "| a | b |\n|---|---|\n| 1 | 2 |",
            "<table>\n<tr><td>a</td><td>b</td></tr>\n<tr><td>1</td><td>2</td></tr>\n</table>",
        ],
    ]
    checks = """
const cases = %s;
for (const [input, want] of cases) {
  const got = markdown(input);
  if (got !== want) {
    console.error('want ' + JSON.stringify(want) + '\\n got ' + JSON.stringify(got));
    process.exit(1);
  }
}

// 只放行 http/https。`javascript:` 不许生成 <a> —— 这段输出会直接进 DOM。
if (markdown('[坏](javascript:alert(1))').includes('<a')) {
  console.error('javascript: 生成了 <a>'); process.exit(1);
}

// href 是拼进属性的，引号不转义就能从属性里跑出去。
if (markdown('[x](https://a.com/\\"onmouseover=\\"alert(1))').includes('onmouseover=\\"alert')) {
  console.error('href 里的引号没转义'); process.exit(1);
}

// 代码块里的内容不参与渲染。
const code = markdown('```\\n- 不是列表\\n> 不是引用\\n```');
if (code.includes('<ul>') || code.includes('<blockquote>')) {
  console.error('代码块被渲染了'); process.exit(1);
}
""" % json.dumps(cases, ensure_ascii=False)

    result = subprocess.run(
        ["node", "-e", _markdown_source() + checks],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 0, result.stderr
