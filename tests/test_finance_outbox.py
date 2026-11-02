"""Failure boundaries of ledger -> outbox -> acknowledged broker delivery."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
import threading
import time

import pytest
from sqlalchemy import delete, event, func, select, update

from backend.ledger import LedgerConflict, LedgerRepository
from backend.models import FinanceOutbox, ImportBatch, LedgerTransaction
from backend.outbox import OutboxPublisher, backfill_events, event_status
from fa.models import Transaction
from tests.test_backend_ledger import db


def txn(identity="one", amount="0.10", account="card"):
    return Transaction(date(2026, 10, 1), "SHOP #1", Decimal(amount), account, identity, "购物")


def events(db):
    with db.session() as session:
        return session.scalars(select(FinanceOutbox).order_by(FinanceOutbox.id)).all()


def test_only_new_source_rows_produce_immutable_decimal_events(db):
    ledger = LedgerRepository(db)
    rows = [txn("a"), txn("b", "-0.01"), txn("a", account="credit")]
    ledger.import_transactions(rows, source_name="one")
    ledger.import_transactions(rows, source_name="repeat")
    ledger.import_transactions([txn("a"), txn("new")], source_name="overlap")
    emitted = events(db)
    assert len(emitted) == 4
    assert len({row.event_id for row in emitted}) == 4
    assert emitted[1].payload["transaction"]["amount"] == "-0.01"
    assert emitted[0].payload["account"] == "card"
    assert emitted[0].payload["transaction"]["merchant_key"] == "shop"


def test_outbox_insert_failure_rolls_back_whole_ledger_batch(db):
    def fail(*_):
        raise RuntimeError("outbox write unavailable")
    event.listen(FinanceOutbox, "before_insert", fail)
    try:
        with pytest.raises(RuntimeError, match="outbox write"):
            LedgerRepository(db).import_transactions([txn("a"), txn("b")], source_name="bad")
    finally:
        event.remove(FinanceOutbox, "before_insert", fail)
    with db.session() as session:
        for model in (ImportBatch, LedgerTransaction, FinanceOutbox):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_source_conflict_also_rolls_back_new_events(db):
    ledger = LedgerRepository(db)
    ledger.import_transactions([txn()], source_name="first")
    with pytest.raises(LedgerConflict):
        ledger.import_transactions([txn("new"), txn(amount="999")], source_name="conflict")
    assert len(events(db)) == 1


def test_concurrent_import_cannot_publish_same_business_row_twice(db):
    barrier = threading.Barrier(2)
    def execute(_):
        barrier.wait()
        return LedgerRepository(db).import_transactions([txn()], source_name="same")
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(r["imported_rows"] for r in pool.map(execute, range(2))) == 1
    assert len(events(db)) == 1


def test_broker_outage_keeps_committed_ledger_and_retries_same_event(db):
    LedgerRepository(db).import_transactions([txn()], source_name="bill")
    sent = []
    class Transport:
        def publish(self, event):
            sent.append(event)
            if len(sent) == 1:
                raise ConnectionError("password must never be persisted")
    publisher = OutboxPublisher(db, Transport(), retry_base_seconds=0)
    assert publisher.publish_once()
    assert events(db)[0].status == "pending"
    assert events(db)[0].error == "ConnectionError"
    assert len(LedgerRepository(db).list_transactions()) == 1
    assert publisher.publish_once()
    assert sent[0] == sent[1]
    assert events(db)[0].status == "published" and events(db)[0].attempts == 2
    assert publisher.publish_once() is False


def test_delivery_crash_before_ack_and_expired_lease_resends_same_identity(db):
    LedgerRepository(db).import_transactions([txn()], source_name="bill")
    sent = []
    class Transport:
        def publish(self, event):
            sent.append(event)
    publisher = OutboxPublisher(db, Transport())
    claimed = publisher._claim()
    # Simulate successful broker delivery and process death before DB ack.
    Transport().publish(claimed[3])
    with db.session() as session:
        session.execute(update(FinanceOutbox).values(lease_until=time.time() - 1))
    assert publisher.publish_once()
    assert sent[0] == sent[1] and events(db)[0].status == "published"


def test_old_lease_owner_cannot_ack_newer_owners_state(db):
    LedgerRepository(db).import_transactions([txn()], source_name="bill")
    class Transport:
        def publish(self, event):
            with db.session() as session:
                session.execute(update(FinanceOutbox).values(lease_token="replacement", status="publishing"))
    OutboxPublisher(db, Transport()).publish_once()
    assert events(db)[0].lease_token == "replacement"
    assert events(db)[0].published_at is None


def test_two_publishers_do_not_claim_the_same_unexpired_event(db):
    LedgerRepository(db).import_transactions([txn()], source_name="bill")
    barrier = threading.Barrier(2)
    sent = []
    class Transport:
        def publish(self, event):
            sent.append(event)
    def attempt(_):
        barrier.wait()
        return OutboxPublisher(db, Transport()).publish_once()
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, range(2))) == [False, True]
    assert len(sent) == 1 and events(db)[0].status == "published"


def test_backoff_prevents_a_tight_retry_loop(db):
    LedgerRepository(db).import_transactions([txn()], source_name="bill")
    class Transport:
        def publish(self, event):
            raise ConnectionError("offline")
    publisher = OutboxPublisher(db, Transport(), retry_base_seconds=60)
    assert publisher.publish_once()
    assert publisher.publish_once() is False
    assert events(db)[0].attempts == 1


def test_backfill_covers_old_ledger_without_reissuing_existing_events(db):
    LedgerRepository(db).import_transactions([txn("a"), txn("b")], source_name="old")
    preserved = events(db)[0]
    with db.session() as session:
        session.execute(delete(FinanceOutbox).where(FinanceOutbox.id != preserved.id))
    assert backfill_events(db) == 1
    assert backfill_events(db) == 0
    assert events(db)[0].event_id == preserved.event_id
    assert event_status(db)["delivery"] == {"pending": 2, "publishing": 0, "published": 0}
