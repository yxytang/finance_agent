"""Restart, replay and concurrent admission checks without a remote model."""
import json
import os
import threading
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema

from backend.db import Database
from backend.session_store import SessionStore
from web.sessions import (ExecutionQueueFull, ManagerClosed, SessionBusy,
                          SessionManager, stream)


class Agent:
    def __init__(self, confirm, on_event):
        self.messages = []
        self.confirm = confirm
        self.emit = on_event
        self.stopped = threading.Event()

    def send(self, text):
        self.messages.extend([HumanMessage(content=text), AIMessage(content="已核对")])
        self.emit({"type": "answer", "text": "已核对"})

    def stop(self):
        self.stopped.set()


@pytest.fixture(params=["sqlite", "postgres"])
def store(tmp_path, request):
    admin = None
    schema = None
    if request.param == "postgres":
        url = os.environ.get("FINANCE_TEST_DATABASE_URL")
        if not url:
            pytest.skip("set FINANCE_TEST_DATABASE_URL to verify PostgreSQL")
        admin = create_engine(url, isolation_level="AUTOCOMMIT")
        schema = "test_sessions_" + uuid.uuid4().hex
        with admin.connect() as connection:
            connection.execute(CreateSchema(schema))
        parsed = make_url(url)
        url = parsed.set(query={**parsed.query, "options": f"-csearch_path={schema}"})
        db = Database(url.render_as_string(hide_password=False))
    else:
        db = Database(f"sqlite:///{(tmp_path / 'sessions.sqlite3').as_posix()}")
    try:
        db.create_schema()
        yield SessionStore(db)
    finally:
        db.dispose()
        if admin:
            with admin.connect() as connection:
                connection.execute(DropSchema(schema, cascade=True))
            admin.dispose()


def manager(store, **kwargs):
    return SessionManager(store=store, session_factory=Agent, **kwargs)


def payload(frame):
    return json.loads(next(line[6:] for line in frame.splitlines() if line.startswith("data: ")))


def test_completed_messages_and_events_survive_manager_restart(store):
    first = manager(store)
    live = first.create()
    first.submit(live, "本月支出").result(timeout=5)
    first.shutdown()
    second = manager(store)
    restored = second.get(live.id)
    assert [message.content for message in restored.session.messages] == ["本月支出", "已核对"]
    assert restored.summary()["turns"] == 1
    assert not restored.busy
    assert [event["type"] for event in store.events_after(live.id)] == ["turn_start", "answer", "turn_end"]
    second.shutdown()


def test_real_agent_can_continue_restored_langchain_context(store):
    from fa.agent import Session
    observed = []
    class Model:
        def invoke(self, messages):
            observed.append([message.content for message in messages])
            return AIMessage(content="offline answer")
    def factory(confirm, on_event):
        return Session(confirm=confirm, on_event=on_event, tools=[],
                       system_prompt="offline system", model=Model())
    first = SessionManager(store=store, session_factory=factory)
    live = first.create()
    first.submit(live, "first question").result(timeout=5)
    first.shutdown()
    second = SessionManager(store=store, session_factory=factory)
    restored = second.get(live.id)
    second.submit(restored, "follow up").result(timeout=5)
    assert observed[-1] == ["offline system", "first question", "offline answer", "follow up"]
    assert sum(message.type == "system" for message in restored.session.messages) == 1
    second.shutdown()


def test_crashed_turn_releases_busy_without_replaying_tools(store):
    row = store.create("crashed")
    assert store.claim(row["id"])
    store.append(row["id"], {"type": "tool_result", "content": "分类已写入"})
    restarted = manager(store)
    restored = restarted.get("crashed")
    assert restored.session.messages == []
    assert not restored.busy
    events = store.events_after("crashed")
    assert [e["type"] for e in events] == ["tool_result", "error", "confirm_resolved", "turn_end"]
    assert events[-1]["interrupted"]
    # A second startup sees no unfinished claim and does not duplicate recovery.
    assert store.recover_interrupted() == 0
    restarted.shutdown()


def test_sequence_cursor_survives_full_wakeup_queue_and_reconnect(store):
    owner = manager(store)
    live = owner.create()
    listener = live.subscribe(after_seq=2)
    for i in range(30):
        live.emit({"type": "answer", "text": str(i)})
    assert listener.qsize() == 1  # events themselves remain safely in the DB
    connection = stream(live, listener, after_seq=2)
    assert payload(next(connection))["type"] == "connected"
    frames = [next(connection) for _ in range(28)]
    assert [payload(frame)["seq"] for frame in frames] == list(range(3, 31))
    assert all(frame.startswith("id: ") for frame in frames)
    live.emit({"type": "answer", "text": "live"})
    assert payload(next(connection))["seq"] == 31
    connection.close()
    assert not live.listeners
    owner.shutdown()


def test_concurrent_journal_writers_allocate_unique_ordered_sequences(store):
    store.create("shared")
    barrier = threading.Barrier(4)
    errors = []
    def write(number):
        try:
            barrier.wait(timeout=5)
            for _ in range(10):
                store.append("shared", {"type": "answer", "writer": number})
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=write, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not errors
    assert [e["seq"] for e in store.events_after("shared")] == list(range(1, 41))


def test_only_one_concurrent_claim_can_start_same_session(store):
    store.create("claim")
    barrier = threading.Barrier(2)
    claims = []
    def claim():
        barrier.wait(timeout=5)
        claims.append(SessionStore(store.db).claim("claim"))
    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert sorted(claims) == [False, True]
    assert store.get("claim")["busy"]


def test_bounded_admission_and_busy_conflict_do_not_drop_existing_claim(store):
    started = threading.Event()
    release = threading.Event()
    class BlockingAgent(Agent):
        def send(self, text):
            started.set()
            release.wait(timeout=10)
            super().send(text)
    owner = SessionManager(store=store, session_factory=BlockingAgent, max_workers=1, max_pending=1)
    live = owner.create()
    pending = owner.create()
    rejected = owner.create()
    try:
        first = owner.submit(live, "first")
        assert started.wait(5)
        with pytest.raises(SessionBusy):
            owner.submit(live, "duplicate")
        assert store.get(live.id)["busy"]
        second = owner.submit(pending, "second")
        with pytest.raises(ExecutionQueueFull):
            owner.submit(rejected, "third")
        assert not store.get(rejected.id)["busy"]
    finally:
        release.set()
        owner.shutdown()
    first.result(timeout=5)
    second.result(timeout=5)
    assert not store.get(live.id)["busy"]
    assert not store.get(pending.id)["busy"]
    with pytest.raises(ManagerClosed):
        owner.submit(rejected, "late")


def test_confirmation_id_rejects_stale_or_duplicate_answers(store):
    owner = manager(store)
    live = owner.create()
    result = []
    waiter = threading.Thread(target=lambda: result.append(live.ask("修改分类？")))
    waiter.start()
    # Synchronize with the actual request, not a timing-dependent sleep.
    listener = live.subscribe()
    connection = stream(live, listener)
    next(connection)
    request = payload(next(connection))
    assert request["type"] == "confirm_request"
    assert not live.answer(True, "old-confirmation")
    assert live.answer(True, request["confirmation_id"])
    assert not live.answer(False, request["confirmation_id"])
    waiter.join(timeout=5)
    assert result == [True]
    assert store.events_after(live.id)[-1]["type"] == "confirm_resolved"
    connection.close()
    owner.shutdown()


def test_close_denies_pending_confirmation_and_deletes_durable_history(store):
    owner = manager(store)
    live = owner.create()
    result = []
    waiter = threading.Thread(target=lambda: result.append(live.ask("修改分类？")))
    waiter.start()
    connection = stream(live, live.subscribe())
    next(connection)
    assert payload(next(connection))["type"] == "confirm_request"
    owner.close(live.id)
    waiter.join(timeout=5)
    assert result == [False]
    assert store.get(live.id) is None
    assert store.events_after(live.id) == []
    connection.close()
    owner.shutdown()


def test_exception_keeps_last_completed_checkpoint(store):
    owner = manager(store)
    live = owner.create()
    owner.submit(live, "good").result(timeout=5)
    def fail(text):
        live.session.messages.append(HumanMessage(content="unfinished"))
        raise RuntimeError("model failed")
    live.session.send = fail
    owner.submit(live, "bad").result(timeout=5)
    assert [m.content for m in live.session.messages] == ["good", "已核对"]
    assert store.get(live.id)["messages"][0]["data"]["content"] == "good"
    assert not store.get(live.id)["busy"]
    assert store.events_after(live.id)[-1]["type"] == "turn_end"
    owner.shutdown()


def test_stopping_queued_turn_does_not_call_agent(store):
    started = threading.Event()
    release = threading.Event()
    calls = []
    class BlockingAgent(Agent):
        def send(self, text):
            calls.append(text)
            if text == "blocking":
                started.set()
                release.wait(timeout=10)
            super().send(text)
    owner = SessionManager(store=store, session_factory=BlockingAgent, max_workers=1, max_pending=1)
    first = owner.create()
    second = owner.create()
    try:
        running = owner.submit(first, "blocking")
        assert started.wait(5)
        queued = owner.submit(second, "queued")
        assert second.stop()
        release.set()
        running.result(timeout=5)
        queued.result(timeout=5)
        assert calls == ["blocking"]
        assert [e["type"] for e in store.events_after(second.id)] == ["turn_start", "stopped", "turn_end"]
        assert not store.get(second.id)["busy"]
    finally:
        release.set()
        owner.shutdown()


def test_close_cannot_accept_new_turn_between_stop_and_removal(store):
    owner = manager(store)
    live = owner.create()
    stopping = threading.Event()
    release = threading.Event()
    attempted = threading.Event()
    completed = threading.Event()
    results = []
    def paused_stop():
        stopping.set()
        release.wait(timeout=5)
        return False
    live.stop = paused_stop
    closer = threading.Thread(target=lambda: owner.close(live.id))
    def submit():
        attempted.set()
        try:
            owner.submit(live, "should not execute")
            results.append("accepted")
        except ManagerClosed:
            results.append("closed")
        finally:
            completed.set()
    closer.start()
    assert stopping.wait(5)
    submitter = threading.Thread(target=submit)
    submitter.start()
    assert attempted.wait(5)
    try:
        assert not completed.wait(0.1)
    finally:
        release.set()
        closer.join(timeout=5)
        submitter.join(timeout=5)
        owner.shutdown()
    assert results == ["closed"]
    assert store.get(live.id) is None
