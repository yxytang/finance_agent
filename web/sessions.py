"""Bounded Agent execution, durable checkpoints and resumable event delivery.

With SessionStore, the database is authoritative. Bounded listener queues only
wake readers; slow browsers catch up from their last delivered sequence.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import queue
import secrets
import threading
import time

from langchain_core.messages import messages_from_dict, messages_to_dict
from fa.agent import Session
from fa.events import TOOL_RESULT
from web.charts import extract_series

CHART = "chart"
TURN_START = "turn_start"
TURN_END = "turn_end"
CONFIRM_REQUEST = "confirm_request"
CONFIRM_TIMEOUT = 300.0
HEARTBEAT_SECONDS = 15.0
MAX_EVENTS = 2000
MAX_SESSIONS = 50


class SessionBusy(RuntimeError):
    pass


class ExecutionQueueFull(RuntimeError):
    pass


class ManagerClosed(RuntimeError):
    pass


def _iso(stamp):
    return datetime.fromtimestamp(stamp).isoformat(timespec="seconds")


class LiveSession:
    def __init__(self, session_id, *, confirm=True, store=None, session_factory=None, snapshot=None):
        self.id = session_id
        self.lock = threading.Lock()
        self.events = []
        self.listeners = []
        self.store = store
        self.closed = False
        self.busy = False
        self.created_at = time.time()
        self.last_activity = self.created_at
        self.turns = 0
        self._seq = 0
        self._pending = None
        self._confirmation_id = None
        self._answer = False
        self._answered = False
        self._stop_requested = False
        if snapshot:
            for name in ("created_at", "last_activity", "turns", "busy"):
                setattr(self, name, snapshot[name])
            self._seq = snapshot["next_seq"]
        factory = session_factory or (lambda confirm, on_event: Session(confirm=confirm, on_event=on_event))
        self.session = factory(self.ask if confirm else None, self.emit)
        if snapshot and snapshot.get("messages"):
            self.session.messages = messages_from_dict(snapshot["messages"])

    def emit(self, event):
        self._record(event)
        if event.get("type") == TOOL_RESULT:
            series = extract_series(event.get("content", ""))
            if series:
                self._record({"type": CHART, "call_id": event.get("call_id"), **series})

    def _cache_and_notify(self, event):
        with self.lock:
            if self.closed:
                return
            self.events.append(event)
            del self.events[:-MAX_EVENTS]
            self._seq = event.get("seq", self._seq)
            self.last_activity = time.time()
            # Same lock as subscription keeps in-memory replay before live events.
            for listener in self.listeners:
                try:
                    listener.put_nowait(None if self.store else event)
                except queue.Full:
                    pass

    def _record(self, event):
        if self.store:
            payload = self.store.append(self.id, event)
            if payload is not None:
                self._cache_and_notify(payload)
        else:
            self._cache_and_notify(event)

    def subscribe(self, after_seq=0):
        if after_seq < 0:
            raise ValueError("after_seq must be nonnegative")
        with self.lock:
            listener = queue.Queue(maxsize=1 if self.store else 0)
            if not self.store:
                for event in self.events:
                    if not event.get("seq") or event["seq"] > after_seq:
                        listener.put_nowait(event)
            if self.closed:
                listener.put_nowait(None)
            else:
                self.listeners.append(listener)
            return listener

    def unsubscribe(self, listener):
        with self.lock:
            if listener in self.listeners:
                self.listeners.remove(listener)

    def summary(self):
        if self.store:
            row = self.store.get(self.id)
            if row:
                return {"id": self.id, "created_at": _iso(row["created_at"]),
                        "last_activity": _iso(row["last_activity"]), "turns": row["turns"],
                        "busy": row["busy"], "events": row["next_seq"]}
        with self.lock:
            return {"id": self.id, "created_at": _iso(self.created_at),
                    "last_activity": _iso(self.last_activity), "turns": self.turns,
                    "busy": self.busy, "events": len(self.events)}

    def ask(self, summary):
        pending = threading.Event()
        confirmation_id = secrets.token_urlsafe(16)
        with self.lock:
            if self.closed:
                return False
            self._pending = pending
            self._confirmation_id = confirmation_id
            self._answer = False
            self._answered = False
        self.emit({"type": CONFIRM_REQUEST, "summary": summary, "confirmation_id": confirmation_id})
        received = pending.wait(timeout=CONFIRM_TIMEOUT)
        with self.lock:
            answer = bool(received and self._answer)
            if self._pending is pending:
                self._pending = None
                self._confirmation_id = None
        if not received:
            self.emit({"type": "confirm_timeout", "summary": summary,
                       "confirmation_id": confirmation_id,
                       "message": f"等了 {int(CONFIRM_TIMEOUT)} 秒没有答复，按拒绝处理。"})
        self.emit({"type": "confirm_resolved", "confirmation_id": confirmation_id,
                   "allow": answer, "reason": "answered" if received else "timeout"})
        return answer

    def answer(self, allow, confirmation_id=None):
        with self.lock:
            if (self._pending is None or self._answered or self.closed
                    or (confirmation_id is not None and confirmation_id != self._confirmation_id)):
                return False
            self._answer = bool(allow)
            self._answered = True
            self._pending.set()
            return True

    def _deny_pending(self):
        with self.lock:
            if self._pending:
                self._answer = False
                self._answered = True
                self._pending.set()

    def stop(self):
        with self.lock:
            running = self.busy
            if running:
                self._stop_requested = True
        if not running:
            return False
        self._deny_pending()
        self.session.stop()
        return True

    def run(self, text):
        with self.lock:
            if self.closed:
                if self.store:
                    self.store.release(self.id)
                return
            self.busy = True
            self.turns += 1
        checkpoint = None
        before = list(getattr(self.session, "messages", []))
        try:
            if self.store:
                self.store.started(self.id)
            self.emit({"type": TURN_START, "text": text})
            with self.lock:
                stopped = self._stop_requested
            if stopped:
                self.emit({"type": "stopped", "text": "已停止，这一轮尚未执行。"})
            else:
                self.session.send(text)
            checkpoint = messages_to_dict(getattr(self.session, "messages", []))
        except Exception as exc:
            if hasattr(self.session, "messages"):
                self.session.messages = before
            self.emit({"type": "error", "message": f"这一轮崩了：{exc}"})
        finally:
            self._deny_pending()
            # Checkpoint, busy release and end event are one DB transaction.
            try:
                if self.store:
                    payload = self.store.finish(self.id, checkpoint)
                    if payload is not None:
                        self._cache_and_notify(payload)
                else:
                    self.emit({"type": TURN_END})
            finally:
                with self.lock:
                    self.busy = False


class SessionManager:
    def __init__(self, *, confirm=True, store=None, session_factory=None,
                 max_workers=2, max_pending=8, recover_interrupted=True):
        if max_workers < 1 or max_pending < 0:
            raise ValueError("invalid worker capacity")
        self._sessions = {}
        self._lock = threading.RLock()
        self._confirm = confirm
        self.store = store
        self._factory = session_factory
        self._closed = False
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="finance-turn")
        self._slots = threading.BoundedSemaphore(max_workers + max_pending)
        if self.store and recover_interrupted:
            self.store.recover_interrupted()

    def _live(self, session_id, snapshot=None):
        return LiveSession(session_id, confirm=self._confirm, store=self.store,
                           session_factory=self._factory, snapshot=snapshot)

    def create(self):
        with self._lock:
            if self._closed:
                raise ManagerClosed("服务正在关闭")
            count = self.store.count() if self.store else len(self._sessions)
            if count >= MAX_SESSIONS:
                raise RuntimeError(f"会话数到上限了（{MAX_SESSIONS}）。先关掉几个再开新的。")
            session_id = secrets.token_urlsafe(12)
            snapshot = self.store.create(session_id) if self.store else None
            try:
                live = self._live(session_id, snapshot)
            except Exception:
                if self.store:
                    self.store.delete(session_id)
                raise
            self._sessions[live.id] = live
            return live

    def get(self, session_id):
        with self._lock:
            live = self._sessions.get(session_id)
            if live is None and self.store:
                snapshot = self.store.get(session_id)
                if snapshot:
                    live = self._live(session_id, snapshot)
                    self._sessions[session_id] = live
            return live

    def list(self):
        if self.store:
            return [live for row in self.store.list() if (live := self.get(row["id"])) is not None]
        with self._lock:
            items = list(self._sessions.values())
        return sorted(items, key=lambda live: live.last_activity, reverse=True)

    def submit(self, live, text):
        with self._lock:
            if self._closed:
                raise ManagerClosed("服务正在关闭")
            if self._sessions.get(live.id) is not live or live.closed:
                raise ManagerClosed("会话已关闭")
            if not self._slots.acquire(blocking=False):
                raise ExecutionQueueFull("任务队列已满，请稍后重试")
            claimed = False
            try:
                with live.lock:
                    if live.busy or (self.store and not self.store.claim(live.id)):
                        raise SessionBusy("上一轮还没跑完")
                    live.busy = True
                    live._stop_requested = False
                    claimed = True
                future = self._executor.submit(live.run, text)
            except Exception:
                if claimed:
                    with live.lock:
                        live.busy = False
                    if self.store:
                        self.store.release(live.id)
                self._slots.release()
                raise
            future.add_done_callback(lambda _: self._slots.release())
            return future

    def drop(self, session_id):
        with self._lock:
            live = self._sessions.pop(session_id, None)
            if live:
                with live.lock:
                    live.closed = True
                    for listener in live.listeners:
                        try:
                            listener.put_nowait(None)
                        except queue.Full:
                            pass
            if self.store:
                self.store.delete(session_id)

    def close(self, session_id):
        # Serialize stop + removal with submit. Otherwise an idle session could
        # accept a new turn in the gap after stop() and run after being closed.
        with self._lock:
            live = self.get(session_id)
            if live is None:
                return False
            running = live.stop()
            live._deny_pending()
            self.drop(session_id)
            return running

    def count(self):
        if self.store:
            return self.store.count()
        with self._lock:
            return len(self._sessions)

    def shutdown(self, wait=True):
        with self._lock:
            self._closed = True
            items = list(self._sessions.values())
        for live in items:
            live.stop()
            live._deny_pending()
        # Drain queued turns so durable claims are cleared.
        self._executor.shutdown(wait=wait, cancel_futures=False)


def stream(live, listener, after_seq=0):
    if after_seq < 0:
        raise ValueError("after_seq must be nonnegative")
    cursor = after_seq
    try:
        yield _sse({"type": "connected", "session": live.id})
        while not live.closed:
            if live.store:
                batch = live.store.events_after(live.id, cursor)
                if batch:
                    for event in batch:
                        cursor = event["seq"]
                        yield _sse(event)
                    continue
            try:
                event = listener.get(timeout=HEARTBEAT_SECONDS)
            except queue.Empty:
                yield ": 心跳\n\n"
                continue
            if not live.store and event is not None:
                yield _sse(event)
    finally:
        live.unsubscribe(listener)


def _sse(payload):
    prefix = f"id: {payload['seq']}\n" if "seq" in payload else ""
    return f"{prefix}data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def now():
    return time.time()
