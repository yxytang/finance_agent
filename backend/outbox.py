"""Transactional finance events and a broker-independent leased publisher.

Sending and marking published are deliberately at-least-once. If the process
dies between them, the immutable event is sent again with the same event_id.
No network operation is performed inside a ledger transaction.
"""
from __future__ import annotations

from copy import deepcopy
import secrets
import time
import uuid

from sqlalchemy import and_, func, or_, select, update

from backend.models import FinanceOutbox, ImportBatch, LedgerTransaction, ProcessedFinanceEvent

IMPORT_EVENT = "ledger.transaction_imported.v1"


def _insert(db, model):
    if db.engine.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert(model)


def import_event(txn, *, source_id: int, batch_id: str, occurred_at: float | None = None) -> dict:
    """Money is a decimal string on the wire, never a JSON float."""
    from fa.normalize import normalize
    return {
        "event_id": uuid.uuid4().hex,
        "event_type": IMPORT_EVENT,
        "schema_version": 1,
        "account": txn.account,
        "occurred_at": time.time() if occurred_at is None else occurred_at,
        "transaction": {"id": source_id, "txn_id": txn.txn_id,
                        "date": txn.date.isoformat(), "merchant": txn.merchant,
                        "merchant_key": normalize(txn.merchant), "amount": str(txn.amount),
                        "category": txn.category, "batch_id": batch_id},
    }


def enqueue_import_event(session, txn, *, source_id: int, batch_id: str) -> None:
    event = import_event(txn, source_id=source_id, batch_id=batch_id)
    session.add(FinanceOutbox(event_id=event["event_id"], event_type=IMPORT_EVENT,
                             source_id=source_id, account=txn.account, payload=event,
                             created_at=event["occurred_at"], available_at=event["occurred_at"]))


def backfill_events(db) -> int:
    """Add missing historical events once, without changing source transactions."""
    inserted = 0
    cursor = 0
    while True:
        with db.session() as session:
            rows = session.execute(select(LedgerTransaction, ImportBatch.created_at).join(
                ImportBatch, LedgerTransaction.batch_id == ImportBatch.id
            ).where(LedgerTransaction.id > cursor).order_by(LedgerTransaction.id).limit(500)).all()
            if not rows:
                return inserted
            for txn, occurred_at in rows:
                event = import_event(txn, source_id=txn.id, batch_id=txn.batch_id, occurred_at=occurred_at)
                result = session.execute(_insert(db, FinanceOutbox).values(
                    event_id=event["event_id"], event_type=IMPORT_EVENT, source_id=txn.id,
                    account=txn.account, payload=event, status="pending", attempts=0,
                    available_at=time.time(), created_at=occurred_at,
                ).on_conflict_do_nothing(index_elements=["event_type", "source_id"]).returning(FinanceOutbox.id))
                inserted += result.scalar_one_or_none() is not None
            cursor = rows[-1][0].id


class OutboxPublisher:
    def __init__(self, db, transport, *, lease_seconds: float = 30, retry_base_seconds: float = 1):
        if lease_seconds <= 0 or retry_base_seconds < 0:
            raise ValueError("Invalid outbox publisher limits")
        self.db, self.transport = db, transport
        self.lease_seconds, self.retry_base_seconds = lease_seconds, retry_base_seconds

    def _claim(self):
        now = time.time()
        eligible = or_(and_(FinanceOutbox.status == "pending", FinanceOutbox.available_at <= now),
                       and_(FinanceOutbox.status == "publishing", FinanceOutbox.lease_until <= now))
        with self.db.session() as session:
            candidates = session.scalars(select(FinanceOutbox.id).where(eligible)
                                         .order_by(FinanceOutbox.id).limit(20)).all()
            for event_id in candidates:
                token = secrets.token_hex(16)
                changed = session.execute(update(FinanceOutbox).where(
                    FinanceOutbox.id == event_id, eligible
                ).values(status="publishing", attempts=FinanceOutbox.attempts + 1,
                         lease_until=now + self.lease_seconds, lease_token=token))
                if changed.rowcount:
                    row = session.get(FinanceOutbox, event_id)
                    return row.id, token, row.attempts, deepcopy(row.payload)
        return None

    def publish_once(self) -> bool:
        claimed = self._claim()
        if claimed is None:
            return False
        row_id, token, attempts, event = claimed
        try:
            self.transport.publish(event)
        except Exception as exc:
            # Persist a safe error class, not broker URLs or credentials.
            delay = min(60, self.retry_base_seconds * 2 ** min(attempts - 1, 6))
            with self.db.session() as session:
                session.execute(update(FinanceOutbox).where(
                    FinanceOutbox.id == row_id, FinanceOutbox.status == "publishing",
                    FinanceOutbox.lease_token == token,
                ).values(status="pending", available_at=time.time() + delay,
                         lease_until=None, lease_token=None, error=type(exc).__name__))
        else:
            with self.db.session() as session:
                session.execute(update(FinanceOutbox).where(
                    FinanceOutbox.id == row_id, FinanceOutbox.status == "publishing",
                    FinanceOutbox.lease_token == token,
                ).values(status="published", published_at=time.time(), error=None,
                         lease_until=None, lease_token=None))
        return True


def event_status(db) -> dict:
    with db.session() as session:
        counts = dict(session.execute(select(FinanceOutbox.status, func.count()).group_by(FinanceOutbox.status)).all())
        consumers = dict(session.execute(select(ProcessedFinanceEvent.consumer, func.count()).group_by(
            ProcessedFinanceEvent.consumer)).all())
        latest = session.scalar(select(func.max(FinanceOutbox.published_at)))
    total = sum(counts.values())
    return {"delivery": {name: counts.get(name, 0) for name in ("pending", "publishing", "published")},
            "total_events": total, "processed_events": consumers, "last_published_at": latest,
            "projections_are_eventually_consistent": True}
