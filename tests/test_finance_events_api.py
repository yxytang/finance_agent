"""HTTP import and async financial views share the real transaction boundary."""
from fastapi.testclient import TestClient

from backend.outbox import OutboxPublisher
from backend.projections import ProjectionService
from tests.test_backend_api import application


def test_finance_views_expose_pending_state_and_authenticate(application):
    with TestClient(application.app) as client:
        for path in ("summary", "anomalies", "events/status"):
            assert client.get(f"/api/finance/{path}").status_code == 401
        csv = ("date,merchant,amount,account,txn_id\n"
               "2026-10-01,SHOP #1,0.10,card,a\n"
               "2026-10-02,SHOP #2,0.10,card,b\n"
               "2026-10-02,SHOP #3,-0.03,card,refund\n"
               "2026-10-02,SHOP #1,0.10,other,c\n")
        assert client.post("/api/ledger/import?token=test", content=csv).json()["imported_rows"] == 4
        before = client.get("/api/finance/summary?token=test").json()
        assert before["items"] == []
        assert before["status"]["delivery"]["pending"] == 4
        assert before["status"]["projections_are_eventually_consistent"] is True
        sent = []
        class Transport:
            def publish(self, event):
                sent.append(event)
        db = application.runtime.db
        publisher = OutboxPublisher(db, Transport())
        while publisher.publish_once():
            pass
        service = ProjectionService(db)
        for event in reversed(sent):
            for projection in ("summary", "anomalies"):
                service.process(projection, event)
                assert service.process(projection, event) is False
        summary = client.get("/api/finance/summary?token=test&account=card").json()["items"]
        assert len(summary) == 1
        assert summary[0]["gross_expense"] == "0.20"
        assert summary[0]["credits"] == "0.03"
        assert summary[0]["net_expense"] == "0.17"
        findings = client.get("/api/finance/anomalies?token=test&account=card").json()["items"]
        assert len(findings) == 1 and findings[0]["txn_ids"] == ["a", "b"]
        assert client.get("/api/finance/anomalies?token=test&account=other").json()["items"] == []
        assert client.post("/api/ledger/import?token=test", content=csv).json()["repeated"]
        status = client.get("/api/finance/events/status?token=test").json()
        assert status["total_events"] == status["delivery"]["published"] == 4
        assert status["processed_events"] == {"summary-v1": 4, "anomalies-v1": 4}
