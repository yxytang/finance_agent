"""Transactional ledger imports and versioned, auditable category corrections.

An imported source record is identified by (account, txn_id). Two charges with
different IDs are retained even when their date, merchant and amount match.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Sequence
import time
import uuid

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from backend.db import Database
from backend.models import CategoryAudit, ImportBatch, LedgerTransaction, MerchantCategory
from fa.ingest import load_transactions
from fa.models import Transaction
from fa.normalize import normalize


class LedgerConflict(ValueError):
    """A source identity or an optimistic version conflicts with stored data."""


class LedgerValidationError(ValueError):
    """The proposed ledger mutation is invalid."""


def _insert(db: Database, model):
    if db.engine.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif db.engine.dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise RuntimeError("Ledger supports SQLite and PostgreSQL")
    return insert(model)


def _source(txn):
    return txn.account, txn.txn_id, txn.date, txn.merchant, txn.amount


def _validate(txn: Transaction) -> None:
    for name, limit in (("account", 128), ("txn_id", 256), ("merchant", 512)):
        value = getattr(txn, name)
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise LedgerValidationError(f"{name} must contain 1..{limit} characters")
    if not txn.amount.is_finite() or txn.amount != txn.amount.quantize(Decimal("0.01")):
        raise LedgerValidationError("amount must be finite with at most two decimal places")
    if abs(txn.amount) >= Decimal("100000000000000000"):
        raise LedgerValidationError("amount exceeds NUMERIC(20,2)")
    if txn.category is not None:
        from fa.config import CATEGORIES
        if txn.category not in CATEGORIES:
            raise LedgerValidationError("unknown category")


class LedgerRepository:
    def __init__(self, db: Database):
        self.db = db

    def import_csv(self, path: Path, *, transactions: Sequence[Transaction] | None = None) -> dict:
        path = Path(path)
        raw = load_transactions(path)
        rows = list(transactions) if transactions is not None else raw
        if [_source(t) for t in raw] != [_source(t) for t in rows]:
            raise LedgerValidationError("categorized rows must preserve the CSV source records")
        return self.import_transactions(rows, source_name=path.name,
                                        content_hash=hashlib.sha256(path.read_bytes()).hexdigest())

    def import_transactions(self, transactions: Sequence[Transaction], *, source_name: str,
                            content_hash: str | None = None) -> dict:
        rows = list(transactions)
        seen = {}
        for txn in rows:
            _validate(txn)
            key = txn.account, txn.txn_id
            if key in seen and _source(seen[key]) != _source(txn):
                raise LedgerConflict(f"source record changed: {txn.account}/{txn.txn_id}")
            seen[key] = txn
        if content_hash is None:
            payload = [[t.account, t.txn_id, t.date.isoformat(), t.merchant, str(t.amount), t.category] for t in rows]
            content_hash = hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()
        batch_id = uuid.uuid4().hex
        with self.db.session() as session:
            inserted = session.execute(_insert(self.db, ImportBatch).values(
                id=batch_id, source_name=str(source_name), content_hash=content_hash,
                imported_rows=0, skipped_rows=0, created_at=time.time(),
            ).on_conflict_do_nothing(index_elements=["content_hash"]).returning(ImportBatch.id)).scalar_one_or_none()
            if inserted is None:
                prior = session.scalar(select(ImportBatch).where(ImportBatch.content_hash == content_hash))
                return {"batch_id": prior.id, "imported_rows": 0,
                        "skipped_rows": len(rows), "repeated": True}
            added = skipped = 0
            for txn in rows:
                inserted_id = session.execute(_insert(self.db, LedgerTransaction).values(
                    txn_id=txn.txn_id, account=txn.account, date=txn.date, merchant=txn.merchant,
                    merchant_key=normalize(txn.merchant), amount=txn.amount,
                    category=txn.category, batch_id=batch_id,
                ).on_conflict_do_nothing(index_elements=["account", "txn_id"]).returning(LedgerTransaction.id)).scalar_one_or_none()
                if inserted_id is not None:
                    added += 1
                    from backend.outbox import enqueue_import_event
                    enqueue_import_event(session, txn, source_id=inserted_id, batch_id=batch_id)
                else:
                    existing = session.scalar(select(LedgerTransaction).where(
                        LedgerTransaction.account == txn.account, LedgerTransaction.txn_id == txn.txn_id))
                    if _source(existing) != _source(txn):
                        raise LedgerConflict(f"source record changed: {txn.account}/{txn.txn_id}")
                    skipped += 1
            session.execute(update(ImportBatch).where(ImportBatch.id == batch_id).values(
                imported_rows=added, skipped_rows=skipped))
            return {"batch_id": batch_id, "imported_rows": added, "skipped_rows": skipped, "repeated": False}

    def list_transactions(self) -> tuple[Transaction, ...]:
        with self.db.session() as session:
            rows = session.execute(select(LedgerTransaction, MerchantCategory.category).outerjoin(
                MerchantCategory, MerchantCategory.merchant_key == LedgerTransaction.merchant_key
            ).order_by(LedgerTransaction.date, LedgerTransaction.id)).all()
            return tuple(Transaction(date=t.date, merchant=t.merchant, amount=t.amount,
                                     account=t.account, txn_id=t.txn_id,
                                     category=override if override is not None else t.category)
                         for t, override in rows)

    def category_state(self, merchant: str) -> dict | None:
        key = normalize(merchant)
        with self.db.session() as session:
            txn = session.scalar(select(LedgerTransaction).where(
                LedgerTransaction.merchant_key == key).order_by(LedgerTransaction.id).limit(1))
            if txn is None:
                return None
            override = session.get(MerchantCategory, key)
            return {"merchant": key, "category": override.category if override else txn.category,
                    "version": override.version if override else 0}

    def correct_category(self, merchant: str, category: str, *, expected_version: int | None = None,
                         request_id: str | None = None) -> dict:
        from fa.config import CATEGORIES
        if category not in CATEGORIES:
            raise LedgerValidationError("unknown category")
        if expected_version is not None and (type(expected_version) is not int or expected_version < 0):
            raise LedgerValidationError("expected_version must be nonnegative")
        if request_id is not None and (not request_id.strip() or len(request_id) > 128):
            raise LedgerValidationError("request_id must contain 1..128 characters")
        key = normalize(merchant)
        for attempt in range(3):
            try:
                with self.db.session() as session:
                    if request_id is not None:
                        prior = session.scalar(select(CategoryAudit).where(CategoryAudit.request_id == request_id))
                        if prior:
                            if prior.merchant_key != key or prior.after_category != category:
                                raise LedgerConflict("request_id was used for a different correction")
                            return dict(prior.result)
                    txn = session.scalar(select(LedgerTransaction).where(
                        LedgerTransaction.merchant_key == key).order_by(LedgerTransaction.id).limit(1))
                    if txn is None:
                        raise LedgerValidationError(f"unknown merchant: {key}")
                    state = session.get(MerchantCategory, key)
                    version = state.version if state else 0
                    before = state.category if state else txn.category
                    if expected_version is not None and version != expected_version:
                        raise LedgerConflict(f"category version changed: expected {expected_version}, current {version}")
                    changed = before != category
                    next_version = version + 1 if changed else version
                    if changed:
                        if state is None:
                            session.add(MerchantCategory(merchant_key=key, category=category, version=1, updated_at=time.time()))
                            session.flush()
                        else:
                            updated_version = session.execute(update(MerchantCategory).where(
                                MerchantCategory.merchant_key == key, MerchantCategory.version == version
                            ).values(category=category, version=next_version, updated_at=time.time())
                                .returning(MerchantCategory.version)).scalar_one_or_none()
                            if updated_version is None:
                                raise LedgerConflict("category version changed; reload before confirming")
                    result = {"merchant": key, "before_category": before, "category": category,
                              "before_version": version, "version": next_version, "changed": changed,
                              "request_id": request_id}
                    if changed or request_id is not None:
                        session.add(CategoryAudit(id=uuid.uuid4().hex, merchant_key=key,
                            before_category=before, after_category=category, before_version=version,
                            after_version=next_version, request_id=request_id, created_at=time.time(), result=result))
                        session.flush()
                    return result
            except IntegrityError as exc:
                # Concurrent initial override / same idempotency key: rollback first,
                # then resolve against the committed version on a fresh transaction.
                if attempt == 2:
                    raise LedgerConflict("concurrent correction; reload and retry") from exc
        raise AssertionError("unreachable")
