"""Exercise real HTTP/lifecycle/DB boundaries with deterministic model output."""
import time
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage


@pytest.fixture
def application(tmp_path, monkeypatch):
    from web import server
    monkeypatch.setenv("FINANCE_DATABASE_URL", f"sqlite:///{(tmp_path / 'backend.sqlite3').as_posix()}")
    monkeypatch.setenv("FINANCE_BOOTSTRAP_CSV", str(tmp_path / "absent.csv"))
    monkeypatch.setenv("FINANCE_DOCUMENT_WORKER", "external")
    monkeypatch.setenv("FINANCE_JOB_STAGING_DIR", str(tmp_path / "staging"))
    monkeypatch.setattr(server, "WEB_TOKEN", "test")
    monkeypatch.setattr(server, "_knowledge_root", lambda: tmp_path / "knowledge")
    monkeypatch.setattr("fa.tools._mcp_tools", lambda *args: ())
    server._rate.clear()
    return server


def wait_turn(server, session_id):
    deadline = time.monotonic() + 5
    while server.manager.get(session_id).busy and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not server.manager.get(session_id).busy


def test_lifespan_persists_messages_events_and_ledger_across_restart(application):
    server = application
    class Model:
        def invoke(self, messages):
            return AIMessage(content="查询已完成")
    with TestClient(server.app) as client:
        assert client.get("/api/health").json()["persistence"] is True
        session_id = client.post("/api/sessions?token=test").json()["id"]
        live = server.manager.get(session_id)
        live.session._model = Model()
        response = client.post(f"/api/sessions/{session_id}/messages?token=test", json={"text": "核对账单"})
        assert response.status_code == 200
        wait_turn(server, session_id)
        before = server.runtime.manager.store.events_after(session_id, 0)
        assert [e["type"] for e in before] == ["turn_start", "step_start", "answer", "turn_end"]
        csv = b"date,merchant,amount,account,txn_id\n2026-10-01,Shop,0.10,card,a\n2026-10-01,Shop,0.20,card,b\n"
        imported = client.post("/api/ledger/import?token=test", content=csv)
        assert imported.status_code == 201 and imported.json()["imported_rows"] == 2
        repeated = client.post("/api/ledger/import?token=test", content=csv)
        assert repeated.json()["repeated"] is True
        assert sum(t.amount for t in server.runtime.ledger.list_transactions()) == Decimal("0.30")
    with TestClient(server.app) as client:
        rows = client.get("/api/sessions?token=test").json()["sessions"]
        assert rows[0]["id"] == session_id and rows[0]["turns"] == 1
        restored = server.manager.get(session_id)
        assert any(m.content == "查询已完成" for m in restored.session.messages)
        assert server.runtime.manager.store.events_after(session_id, 0) == before
        assert len(server.runtime.ledger.list_transactions()) == 2
        assert client.get(f"/api/sessions/{session_id}/events?token=test&after=-1").status_code == 422
        assert client.delete(f"/api/sessions/{session_id}?token=test").status_code == 200
        assert server.runtime.manager.store.events_after(session_id, 0) == []


def test_upload_returns_queue_before_parsing_and_http_cancel_retry(application):
    server = application
    with TestClient(server.app) as client:
        assert client.post("/api/knowledge/files/p.md", content=b"# Budget\n100").status_code == 401
        content = b"# Budget\nMonthly reserve target is 1000. Keep emergency funds for three months."
        response = client.post("/api/knowledge/files/p.md?token=test", content=content)
        assert response.status_code == 202
        job_id = response.json()["id"]
        assert response.json()["status"] == "queued"
        assert not (server._knowledge_root() / "uploads" / "p.md").exists()
        same = client.post("/api/knowledge/files/p.md?token=test", content=content)
        assert same.json()["id"] == job_id
        assert client.post(f"/api/jobs/{job_id}/cancel?token=test").json()["status"] == "cancelled"
        assert client.post(f"/api/jobs/{job_id}/retry?token=test").status_code == 202
        assert server.runtime.jobs.run_once()
        completed = client.get(f"/api/jobs/{job_id}?token=test").json()
        assert completed["status"] == "succeeded" and completed["result"]["chunks"] == 1
        assert client.post(f"/api/jobs/{job_id}/retry?token=test").status_code == 409
        assert client.get("/api/jobs/unknown?token=test").status_code == 404


def test_csv_conflict_rolls_back_http_import(application):
    server = application
    with TestClient(server.app) as client:
        prefix = "date,merchant,amount,account,txn_id\n"
        assert client.post("/api/ledger/import?token=test", content=prefix + "2026-10-01,Shop,1,card,a\n").status_code == 201
        conflict = prefix + "2026-10-02,Other,2,card,b\n2026-10-01,Shop,99,card,a\n"
        assert client.post("/api/ledger/import?token=test", content=conflict).status_code == 409
        assert [t.txn_id for t in server.runtime.ledger.list_transactions()] == ["a"]


def test_db_correction_updates_all_tools_and_keeps_audit(application):
    server = application
    from fa.tools import build_tools
    from backend.models import CategoryAudit
    from sqlalchemy import select
    with TestClient(server.app) as client:
        csv = "date,merchant,amount,account,txn_id\n2026-10-01,STARBUCKS #1,10,card,a\n"
        assert client.post("/api/ledger/import?token=test", content=csv).status_code == 201
        repo = server.runtime.ledger
        tools = {t.name: t for t in build_tools(lambda _: True, repo.list_transactions, repository=repo, use_mcp=False)}
        assert "已改" in tools["correct_category"].invoke({"merchant": "STARBUCKS #1", "category": "购物"})
        assert repo.list_transactions()[0].category == "购物"
        with server.runtime.db.session() as db_session:
            audits = db_session.scalars(select(CategoryAudit)).all()
            assert len(audits) == 1 and audits[0].after_version == 1


def test_http_confirmation_requires_current_id(application):
    import threading
    server = application
    with TestClient(server.app) as client:
        session_id = client.post("/api/sessions?token=test").json()["id"]
        live = server.manager.get(session_id)
        answers = []
        waiter = threading.Thread(target=lambda: answers.append(live.ask("修改分类")))
        waiter.start()
        try:
            deadline = time.monotonic() + 3
            pending = []
            while not pending and time.monotonic() < deadline:
                pending = [e for e in server.manager.store.events_after(session_id) if e["type"] == "confirm_request"]
                time.sleep(0.01)
            assert pending
            url = f"/api/sessions/{session_id}/confirm?token=test"
            assert client.post(url, json={"allow": True}).status_code == 422
            assert client.post(url, json={"allow": True, "confirmation_id": "old"}).json()["accepted"] is False
            current = pending[-1]["confirmation_id"]
            assert client.post(url, json={"allow": True, "confirmation_id": current}).json()["accepted"] is True
        finally:
            live._deny_pending()
            waiter.join(3)
        assert answers == [True]
