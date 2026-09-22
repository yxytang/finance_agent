"""Web 端的会话管理 —— 把 agent 的事件流转成可以推给浏览器的东西。

## 权限确认怎么跨 HTTP 工作

`correct_category` 会先问一句再改东西（Day 4 定的），而那个回调是**同步**的：
它在 `session.send()` 里面被调用，必须当场返回一个 bool。

浏览器那边却是异步的 —— 用户看到弹窗、想几秒、点一下，这中间隔着一次网络往返。

所以这里用一条阻塞等待把两边接起来：

    工具线程                          浏览器
    ─────────                         ──────
    confirm() 被调用
      推一条 confirm_request 事件 ──▶  弹出确认框
      event.wait(timeout=...)          …用户想几秒…
      ◀──────────────────────────────  POST /confirm
      返回 True/False

**超时即拒绝。** 浏览器关掉了、网络断了、用户走开了 —— 这些情况下等待必须
结束，而且必须结束在「不允许」那一侧。一个等不到答复就放行的实现，等于把
确认这件事取消了。

## 为什么服务器要留一份事件日志

SSE 没有回放。刷新页面之后新连接是「从此刻起」的 —— 而 agent 可能已经跑了
一半，那些步骤就永远看不到了。

所以每个会话留一份事件日志，新连接**先回放再续播**。这同时也是「刷新页面
历史还在」的实现方式，不需要客户端自己存。

日志是有上限的：一个跑了几十轮的会话能攒下几兆的事件，而其中绝大部分不会
再被看。超了就丢最老的。
"""

import queue
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from fa.agent import Session
from fa.events import TOOL_RESULT
from web.charts import extract_series

# 这个也是**传输层**的事件：agent 不知道有图表这回事，是服务器从工具结果里
# 认出分组表之后补推的。见 `_record` 上面那段说明。
CHART = "chart"

# 这两个是**传输层**的事件，不是 agent 发的 —— agent 只负责 `step_start` /
# `tool_call` / ... 那几个（见 fa/events.py）。
#
# 服务器需要它们，是因为浏览器要知道「这一轮什么时候结束」才能把输入框
# 重新启用。agent 的 `answer` / `error` 事件虽然也标志着结束，但那是 agent
# 的语义，而这里是传输的语义 —— 一轮可能因为异常没有那两个事件。
TURN_START = "turn_start"
TURN_END = "turn_end"

CONFIRM_REQUEST = "confirm_request"

# 确认框最多等 5 分钟。等不到就当用户拒绝。
CONFIRM_TIMEOUT = 300.0

# 心跳间隔。中间有代理时，长时间没有字节的 SSE 连接会被掐掉 ——
# 而 agent 跑一轮可能要几十秒，中间一直在调工具、没有给浏览器发过东西。
HEARTBEAT_SECONDS = 15.0

# 每个会话最多留多少条事件。超了丢最老的。
MAX_EVENTS = 2000

# 同时最多开多少个会话。有写权限的 agent 不能让人随便开。
MAX_SESSIONS = 50


def _iso(stamp: float) -> str:
    """时间戳 → ISO 字符串。浏览器 `new Date(...)` 直接能解。

    用**本地时间**而不是 UTC：这个列表是给人看的，`fromtimestamp` 出的是本机
    时区。要给机器比对才需要 UTC。
    """
    return datetime.fromtimestamp(stamp).isoformat(timespec="seconds")


class LiveSession:
    """一个浏览器会话对着一个 agent Session。

    故意写成普通类而不是 dataclass：`Session` 的构造需要 `confirm` 回调，而那个
    回调要绑在**这个对象**上。dataclass 的字段在 `__init__` 里依次赋值，拿不到
    「已经构造了一半的 self」；普通类里 `self.ask` 在构造时随手就有。
    """

    def __init__(self, session_id: str, *, confirm: bool = True):
        self.id = session_id
        self.lock = threading.Lock()
        self.events: list[dict] = []
        self.listeners: list[queue.Queue] = []
        self.busy = False

        # 会话列表要用的元数据。**没有它，浏览器就只能自己记一个 id** ——
        # 而 id 一丢（刷新、换标签页、换设备）那个会话就再也找不回来了，
        # 尽管服务端的回放明明还在，却没有任何入口能走回去。
        self.created_at = time.time()
        self.last_activity = self.created_at
        self.turns = 0

        self._pending: threading.Event | None = None
        self._answer = False

        # `on_event` 必须在这里接上。忘了接的话 agent 照常跑，但所有事件
        # （step_start / tool_call / tool_result / answer）全部丢进虚空 ——
        # 浏览器只看得到 turn_start 和 turn_end：界面一片空白，而**没有任何
        # 报错**。这是那种「看起来像前端坏了、其实是后端没接线」的问题。
        self.session = Session(
            confirm=self.ask if confirm else None,
            on_event=self.emit,
        )

    # --- 事件播报 -------------------------------------------------------

    def emit(self, event: dict) -> None:
        """记进日志 + 推给所有打开的连接。

        这个方法是从**工具线程**里被调的（agent 的 `on_event`），所以日志和
        监听器列表都要加锁。
        """
        self._record(event)

        # 工具结果里如果是一张分组表，顺手再推一份**结构化**的数据给前端画图。
        #
        # 放在服务端做而不是让前端解字符串，是为了让 agent 和工具层完全不用知道
        # 「有前端这回事」—— 它们照旧只返回给模型看的文本。也放在服务端而不是
        # 前端，是因为解析这件事要有测试钉住（见 web/charts.py），而前端的代码
        # 在这个项目里没有测试。
        if event.get("type") == TOOL_RESULT:
            series = extract_series(event.get("content", ""))
            if series:
                self._record({"type": CHART, "call_id": event.get("call_id"), **series})

    def _record(self, event: dict) -> None:
        with self.lock:
            self.events.append(event)
            if len(self.events) > MAX_EVENTS:
                del self.events[: len(self.events) - MAX_EVENTS]
            self.last_activity = time.time()
            listeners = list(self.listeners)

        for listener in listeners:
            listener.put(event)

    def subscribe(self) -> queue.Queue:
        """打开一条新连接。**先拿到回放，再加入监听**。

        顺序反了的话，两步之间产生的事件会既不在回放里、也没推过来 ——
        表现为「刷新之后少了一步」，而且只在时间凑巧时出现（间歇性 bug）。
        """
        with self.lock:
            backlog = list(self.events)
            listener: queue.Queue = queue.Queue()
            self.listeners.append(listener)

        for event in backlog:
            listener.put(event)
        return listener

    def unsubscribe(self, listener: queue.Queue) -> None:
        with self.lock:
            if listener in self.listeners:
                self.listeners.remove(listener)

    def summary(self) -> dict:
        """会话列表里的一行。**不含对话内容** —— 列表只需要知道「有这么个
        会话、什么时候动过」，正文要靠连上去回放。"""
        with self.lock:
            return {
                "id": self.id,
                "created_at": _iso(self.created_at),
                "last_activity": _iso(self.last_activity),
                "turns": self.turns,
                "busy": self.busy,
                "events": len(self.events),
            }

    # --- 权限确认 -------------------------------------------------------

    def ask(self, summary: str) -> bool:
        """工具线程在这里停下来等浏览器回答。超时即拒绝。"""
        pending = threading.Event()
        with self.lock:
            self._pending = pending
            self._answer = False

        self.emit({"type": CONFIRM_REQUEST, "summary": summary})

        if not pending.wait(timeout=CONFIRM_TIMEOUT):
            # 见模块 docstring：等不到答复必须落在「不允许」那一侧。
            self.emit(
                {
                    "type": "confirm_timeout",
                    "summary": summary,
                    "message": f"等了 {int(CONFIRM_TIMEOUT)} 秒没有答复，按拒绝处理。",
                }
            )
            with self.lock:
                self._pending = None
            return False

        with self.lock:
            answer = self._answer
            self._pending = None
        return answer

    def answer(self, allow: bool) -> bool:
        """浏览器点了按钮。返回是否真的有人等着。"""
        with self.lock:
            pending = self._pending
            self._answer = allow
        if pending is None:
            return False
        pending.set()
        return True

    def stop(self) -> bool:
        """请求停掉正在跑的那一轮。返回「当时确实有一轮在跑」。

        只是**转发**给 agent 的 `Session.stop()` —— 只有那里知道怎么安全地停
        （见它的说明：模型调用拦不住，所以会停在当前这一步之后）。

        判 `busy` 和转发之间有缝：这一轮可能正好在这中间跑完。后果是往一个已经
        空闲的会话上设了停止信号，而下一次 `send()` 开头会清掉它 —— 无害，
        不值得为它加锁把两个线程串起来。
        """
        with self.lock:
            if not self.busy:
                return False
        self.session.stop()
        return True

    # --- 跑一轮 ---------------------------------------------------------

    def run(self, text: str) -> None:
        """在后台线程里跑一轮。做完一定发 `turn_end` —— 哪怕炸了。

        不发的话浏览器的输入框会永远禁用着，而用户完全不知道发生了什么。
        """
        with self.lock:
            self.turns += 1
        self.emit({"type": TURN_START, "text": text})
        try:
            self.session.send(text)
        except Exception as exc:  # noqa: BLE001 - 一定要把状态机推回可用
            self.emit({"type": "error", "message": f"这一轮崩了：{exc}"})
        finally:
            with self.lock:
                self.busy = False
            self.emit({"type": TURN_END})


class SessionManager:
    """一堆 LiveSession。有上限，因为每个会话都可能改文件。"""

    def __init__(self, *, confirm: bool = True):
        self._sessions: dict[str, LiveSession] = {}
        self._lock = threading.Lock()
        self._confirm = confirm

    def create(self) -> LiveSession:
        with self._lock:
            if len(self._sessions) >= MAX_SESSIONS:
                raise RuntimeError(
                    f"会话数到上限了（{MAX_SESSIONS}）。先关掉几个再开新的。"
                )
            live = LiveSession(secrets.token_urlsafe(12), confirm=self._confirm)
            self._sessions[live.id] = live
        return live

    def get(self, session_id: str) -> LiveSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def list(self) -> list[LiveSession]:
        """所有会话，最近活动的在前。

        先复制再排序：排序要读每个会话的 `last_activity`，不该在握着 manager
        的锁时去做（那会把所有请求串起来）。
        """
        with self._lock:
            items = list(self._sessions.values())
        items.sort(key=lambda live: live.last_activity, reverse=True)
        return items

    def drop(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)


def stream(live: LiveSession, listener: queue.Queue):
    """把一条监听队列变成 SSE 的字节流。

    心跳是必要的，不是装饰：中间有代理时，长时间没有字节的连接会被掐掉 ——
    而 agent 跑一轮要几十秒，中间一直在调工具、没给浏览器发过任何东西。
    没有心跳的话，用户看到的是「跑到一半连接断了」。
    """
    try:
        yield _sse({"type": "connected", "session": live.id})
        while True:
            try:
                event = listener.get(timeout=HEARTBEAT_SECONDS)
            except queue.Empty:
                # 注释行：SSE 规范里以 `:` 开头的行会被客户端忽略，
                # 但它**是字节**，足够让中间那些东西认为连接还活着。
                yield ": 心跳\n\n"
                continue
            yield _sse(event)
    finally:
        live.unsubscribe(listener)


def _sse(payload: dict) -> str:
    """一个 SSE 帧。"""
    import json

    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def now() -> float:
    return time.time()
