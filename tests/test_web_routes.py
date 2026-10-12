"""HTTP 拒绝请求时，不应占用会话或启动模型调用。"""

import pytest

pytest.importorskip("fastapi")

from fastapi import HTTPException
from starlette.requests import Request

from web import server
from web.sessions import SessionManager


def test_rate_limited_request_leaves_session_idle(monkeypatch):
    manager = SessionManager(confirm=False)
    live = manager.create()
    monkeypatch.setattr(server, "manager", manager)
    monkeypatch.setattr(server, "WEB_TOKEN", "")
    monkeypatch.setattr(server, "TURNS_PER_MINUTE", 0)
    request = Request({"type": "http", "headers": [], "query_string": b""})

    with pytest.raises(HTTPException) as error:
        server.send(live.id, server.Message(text="查看账单范围"), request)

    assert error.value.status_code == 429
    assert live.busy is False
    assert live.events == []
    # 限流结束后，同一会话仍然能接收问题。
    monkeypatch.setattr(server, "TURNS_PER_MINUTE", 6)
    class NoopThread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(server.threading, "Thread", NoopThread)
    assert server.send(live.id, server.Message(text="查看账单范围"), request)["accepted"]
