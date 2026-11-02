"""Transactional, rebuildable views of imported financial events.

Kafka offsets are committed by the transport only after :meth:`process`
returns. Event receipts, source receipts and financial results commit together;
redelivery after a database commit therefore cannot add a charge twice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re
import time

from sqlalchemy import delete, select, update

from backend.ledger import _insert
from backend.models import (
    AnomalyTransaction, DuplicateChargeFinding, MonthlyFinanceSummary,
    ProcessedFinanceEvent, ProjectedFinanceTransaction, ProjectionAccount,
)
from fa.anomalies import DEFAULT_WINDOW_DAYS, find_duplicates
from fa.models import Transaction
from fa.normalize import normalize


CONSUMERS = {"summary": "summary-v1", "anomalies": "anomalies-v1"}
EVENT_TYPE = "ledger.transaction_imported.v1"
ZERO = Decimal("0.00")


class InvalidFinanceEvent(ValueError):
    """A message is unsupported, malformed or conflicts with prior evidence."""


class ProjectionAmountOverflow(OverflowError):
    """An exact aggregate does not fit the configured database money storage."""


@dataclass(frozen=True)
class ValidatedEvent:
    event_id: str
    account: str
    transaction: Transaction
    ledger_id: int
    merchant_key: str
    payload_hash: str
    source_hash: str


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _string(value, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise InvalidFinanceEvent(f"{name} must contain 1..{maximum} characters")
    return value


def validate_event(event: dict) -> ValidatedEvent:
    """Validate untrusted JSON without silently rounding monetary amounts."""
    if not isinstance(event, dict):
        raise InvalidFinanceEvent("event must be an object")
    event_id = event.get("event_id")
    if not isinstance(event_id, str) or re.fullmatch(r"[0-9a-f]{32}", event_id) is None:
        raise InvalidFinanceEvent("event_id must be a lowercase 32-character hex ID")
    if event.get("event_type") != EVENT_TYPE:
        raise InvalidFinanceEvent("unsupported financial event type")
    if type(event.get("schema_version")) is not int or event["schema_version"] != 1:
        raise InvalidFinanceEvent("unsupported financial event schema version")
    stamp = event.get("occurred_at")
    try:
        valid_stamp = type(stamp) in (int, float) and math.isfinite(stamp) and stamp >= 0
    except OverflowError:
        valid_stamp = False
    if not valid_stamp:
        raise InvalidFinanceEvent("occurred_at must be a finite nonnegative timestamp")
    account = _string(event.get("account"), "account", 128)
    source = event.get("transaction")
    if not isinstance(source, dict):
        raise InvalidFinanceEvent("transaction must be an object")
    ledger_id = source.get("id")
    if type(ledger_id) is not int or not 0 < ledger_id < 2**63:
        raise InvalidFinanceEvent("transaction.id must be a positive integer")
    txn_id = _string(source.get("txn_id"), "transaction.txn_id", 256)
    merchant = _string(source.get("merchant"), "transaction.merchant", 512)
    merchant_key = source.get("merchant_key")
    if not isinstance(merchant_key, str) or len(merchant_key) > 512 or merchant_key != normalize(merchant):
        raise InvalidFinanceEvent("transaction.merchant_key does not match merchant")
    batch_id = _string(source.get("batch_id"), "transaction.batch_id", 32)
    category = source.get("category")
    if category is not None:
        _string(category, "transaction.category", 64)
    try:
        raw_date = source.get("date")
        if not isinstance(raw_date, str):
            raise ValueError("date must be a string")
        happened = date.fromisoformat(raw_date)
        if happened.isoformat() != raw_date:
            raise ValueError("date must use YYYY-MM-DD")
    except ValueError as exc:
        raise InvalidFinanceEvent("transaction.date must use YYYY-MM-DD") from exc
    raw_amount = source.get("amount")
    if not isinstance(raw_amount, str) or not 0 < len(raw_amount) <= 64:
        raise InvalidFinanceEvent("transaction.amount must be a decimal string")
    try:
        amount = Decimal(raw_amount)
        if (not amount.is_finite() or abs(amount) >= Decimal("100000000000000000")
                or amount != amount.quantize(Decimal("0.01"))):
            raise InvalidOperation
        amount = amount.quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise InvalidFinanceEvent("transaction.amount must be finite exact cents within range") from exc
    canonical_source = {
        "account": account, "id": ledger_id, "txn_id": txn_id,
        "date": happened.isoformat(), "merchant": merchant,
        "merchant_key": merchant_key, "amount": str(amount),
        "category": category, "batch_id": batch_id,
    }
    # Include the entire JSON envelope in the event hash: reusing an event ID
    # with a changed payload must fail rather than silently hide corruption.
    try:
        payload_hash = _hash(event)
    except (TypeError, ValueError, OverflowError) as exc:
        raise InvalidFinanceEvent("event must contain finite JSON data") from exc
    return ValidatedEvent(
        event_id, account, Transaction(happened, merchant, amount, account, txn_id, category),
        ledger_id, merchant_key, payload_hash, _hash(canonical_source),
    )


class ProjectionService:
    def __init__(self, db):
        self.db = db

    def process(self, projection: str, event: dict) -> bool:
        """Apply a new source once; return False for a safe redelivery."""
        if projection not in CONSUMERS:
            raise ValueError("projection must be summary or anomalies")
        consumer = CONSUMERS[projection]
        item = validate_event(event)
        now = time.time()
        with self.db.session() as session:
            receipt = session.execute(_insert(self.db, ProcessedFinanceEvent).values(
                consumer=consumer, event_id=item.event_id, payload_hash=item.payload_hash,
                processed_at=now,
            ).on_conflict_do_nothing(index_elements=["consumer", "event_id"])
                .returning(ProcessedFinanceEvent.event_id)).scalar_one_or_none()
            if receipt is None:
                prior = session.get(ProcessedFinanceEvent, (consumer, item.event_id))
                if prior.payload_hash != item.payload_hash:
                    raise InvalidFinanceEvent("event_id was reused with a different payload")
                return False
            source = session.execute(_insert(self.db, ProjectedFinanceTransaction).values(
                consumer=consumer, ledger_id=item.ledger_id, event_id=item.event_id,
                source_hash=item.source_hash,
            ).on_conflict_do_nothing(index_elements=["consumer", "ledger_id"])
                .returning(ProjectedFinanceTransaction.ledger_id)).scalar_one_or_none()
            if source is None:
                prior = session.get(ProjectedFinanceTransaction, (consumer, item.ledger_id))
                if prior.source_hash != item.source_hash:
                    raise InvalidFinanceEvent("ledger identity was reused with different source data")
                return False
            # UPDATE obtains an account row lock in PostgreSQL. SQLite writers
            # serialize at database level. Anomaly reads and writes below are
            # therefore not based on competing stale account snapshots.
            session.execute(_insert(self.db, ProjectionAccount).values(
                consumer=consumer, account=item.account, revision=0,
            ).on_conflict_do_nothing(index_elements=["consumer", "account"]))
            session.execute(update(ProjectionAccount).where(
                ProjectionAccount.consumer == consumer,
                ProjectionAccount.account == item.account,
            ).values(revision=ProjectionAccount.revision + 1))
            if projection == "summary":
                self._summarize(session, item, now)
            else:
                self._detect_duplicates(session, item, now)
        return True

    def _summarize(self, session, item: ValidatedEvent, now: float) -> None:
        amount = item.transaction.amount
        row = session.get(MonthlyFinanceSummary, (item.account, item.transaction.year_month))
        # The account lock acquired by process() covers this read and write.
        # SQLite integer overflow in a SQL `existing + excluded` expression
        # silently promotes cents to REAL; calculate and range-check in Decimal
        # before binding the exact replacement values instead.
        totals = {
            "gross_expense": (row.gross_expense if row else ZERO) + max(amount, ZERO),
            "credits": (row.credits if row else ZERO) + max(-amount, ZERO),
            "net_expense": (row.net_expense if row else ZERO) + amount,
        }
        for total in totals.values():
            if not total.is_finite() or abs(total) >= Decimal("100000000000000000"):
                raise ProjectionAmountOverflow("financial aggregate exceeds MoneyType amount range")
            if self.db.engine.dialect.name == "sqlite":
                cents = int(total * 100)
                if not -(2**63) <= cents < 2**63:
                    raise ProjectionAmountOverflow("financial aggregate exceeds SQLite exact cents range")
        if row is None:
            session.add(MonthlyFinanceSummary(
                account=item.account, month=item.transaction.year_month,
                **totals, transaction_count=1, updated_at=now,
            ))
        else:
            for name, total in totals.items():
                setattr(row, name, total)
            row.transaction_count += 1
            row.updated_at = now

    def _detect_duplicates(self, session, item: ValidatedEvent, now: float) -> None:
        session.execute(_insert(self.db, AnomalyTransaction).values(
            ledger_id=item.ledger_id, account=item.account,
            txn_id=item.transaction.txn_id, date=item.transaction.date,
            merchant_key=item.merchant_key, amount=item.transaction.amount,
        ).on_conflict_do_nothing(index_elements=["ledger_id"]))
        prior = session.get(AnomalyTransaction, item.ledger_id)
        if (prior.account, prior.txn_id, prior.date, prior.merchant_key, prior.amount) != (
            item.account, item.transaction.txn_id, item.transaction.date,
            item.merchant_key, item.transaction.amount,
        ):
            raise InvalidFinanceEvent("anomaly source identity conflicts with stored transaction")
        rows = session.scalars(select(AnomalyTransaction).where(
            AnomalyTransaction.account == item.account,
            AnomalyTransaction.amount > ZERO,
        ).order_by(AnomalyTransaction.date, AnomalyTransaction.txn_id)).all()
        transactions = [Transaction(r.date, r.merchant_key, r.amount, r.account, r.txn_id)
                        for r in rows]
        findings = find_duplicates(transactions)
        session.execute(delete(DuplicateChargeFinding).where(
            DuplicateChargeFinding.account == item.account,
        ))
        for finding in findings:
            dates = [d.isoformat() for d in finding.dates]
            identity = _hash([item.account, finding.merchant, str(finding.amount),
                              list(finding.txn_ids), dates])
            session.add(DuplicateChargeFinding(
                id=identity, account=item.account, merchant_key=finding.merchant,
                amount=finding.amount, txn_ids=list(finding.txn_ids), dates=dates,
                updated_at=now,
            ))

    def summary(self, account: str | None = None) -> list[dict]:
        statement = select(MonthlyFinanceSummary)
        if account is not None:
            statement = statement.where(MonthlyFinanceSummary.account == account)
        with self.db.session() as session:
            return [{
                "account": row.account, "month": row.month,
                "gross_expense": str(row.gross_expense), "credits": str(row.credits),
                "net_expense": str(row.net_expense), "transaction_count": row.transaction_count,
                "updated_at": row.updated_at,
            } for row in session.scalars(statement.order_by(
                MonthlyFinanceSummary.account, MonthlyFinanceSummary.month,
            ))]

    def anomalies(self, account: str | None = None) -> list[dict]:
        statement = select(DuplicateChargeFinding)
        if account is not None:
            statement = statement.where(DuplicateChargeFinding.account == account)
        with self.db.session() as session:
            return [{
                "id": row.id, "kind": "suspected_duplicate_charge", "account": row.account,
                "merchant": row.merchant_key, "amount": str(row.amount),
                "txn_ids": row.txn_ids, "dates": row.dates,
                "occurrences": len(row.txn_ids), "window_days": DEFAULT_WINDOW_DAYS,
                "updated_at": row.updated_at,
            } for row in session.scalars(statement.order_by(
                DuplicateChargeFinding.account, DuplicateChargeFinding.id,
            ))]
