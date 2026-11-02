"""Shared relational models. Domain calculations remain in :mod:`fa`."""

from __future__ import annotations

from decimal import Decimal
from datetime import date
import time

from sqlalchemy import (
    BigInteger, Boolean, Date, Float, ForeignKey, Index, Integer, JSON,
    Numeric, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class Base(DeclarativeBase):
    pass


class MoneyType(TypeDecorator):
    """PostgreSQL NUMERIC; exact integer cents on SQLite development stores."""

    impl = Numeric(20, 2)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(BigInteger() if dialect.name == "sqlite" else Numeric(20, 2))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        amount = Decimal(str(value))
        if not amount.is_finite() or amount != amount.quantize(Decimal("0.01")):
            raise ValueError("amount must be finite and have at most two decimal places")
        if abs(amount) >= Decimal("100000000000000000"):
            raise ValueError("amount is outside NUMERIC(20,2) range")
        return int(amount * 100) if dialect.name == "sqlite" else amount

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if dialect.name == "sqlite":
            return (Decimal(value) / 100).quantize(Decimal("0.01"))
        return Decimal(value).quantize(Decimal("0.01"))


class SessionRecord(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    last_activity: Mapped[float] = mapped_column(Float, default=time.time)
    turns: Mapped[int] = mapped_column(Integer, default=0)
    busy: Mapped[bool] = mapped_column(Boolean, default=False)
    messages: Mapped[list] = mapped_column(JSON, default=list)
    next_seq: Mapped[int] = mapped_column(Integer, default=0)


class EventRecord(Base):
    __tablename__ = "session_events"
    session_id: Mapped[str] = mapped_column(String(64), ForeignKey("sessions.id", ondelete="CASCADE"), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)


class DocumentJob(Base):
    __tablename__ = "document_jobs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    root: Mapped[str] = mapped_column(Text)
    filename: Mapped[str] = mapped_column(String(160))
    content_hash: Mapped[str] = mapped_column(String(64))
    staged_path: Mapped[str] = mapped_column(Text)
    request_key: Mapped[str] = mapped_column(String(128), unique=True)
    status: Mapped[str] = mapped_column(String(24), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    available_at: Mapped[float] = mapped_column(Float, default=time.time)
    lease_until: Mapped[float | None] = mapped_column(Float, nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (Index("ix_document_jobs_claim", "status", "available_at", "created_at"),)


class ImportBatch(Base):
    __tablename__ = "import_batches"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    source_name: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), unique=True)
    imported_rows: Mapped[int] = mapped_column(Integer)
    skipped_rows: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class LedgerTransaction(Base):
    __tablename__ = "ledger_transactions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    txn_id: Mapped[str] = mapped_column(String(256))
    account: Mapped[str] = mapped_column(String(128))
    date: Mapped[date] = mapped_column(Date)
    merchant: Mapped[str] = mapped_column(String(512))
    merchant_key: Mapped[str] = mapped_column(String(512))
    amount: Mapped[Decimal] = mapped_column(MoneyType())
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    batch_id: Mapped[str] = mapped_column(String(32), ForeignKey("import_batches.id"))
    __table_args__ = (
        UniqueConstraint("account", "txn_id", name="uq_ledger_source_record"),
        Index("ix_ledger_account_date", "account", "date"),
        Index("ix_ledger_date_category", "date", "category"),
        Index("ix_ledger_merchant_key", "merchant_key"),
    )


class MerchantCategory(Base):
    __tablename__ = "merchant_categories"
    merchant_key: Mapped[str] = mapped_column(String(512), primary_key=True)
    category: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)


class CategoryAudit(Base):
    __tablename__ = "category_audits"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    merchant_key: Mapped[str] = mapped_column(String(512))
    before_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    after_category: Mapped[str] = mapped_column(String(64))
    before_version: Mapped[int] = mapped_column(Integer)
    after_version: Mapped[int] = mapped_column(Integer)
    request_id: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    result: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (Index("ix_category_audit_merchant_time", "merchant_key", "created_at"),)


class FinanceOutbox(Base):
    """Immutable import events; delivery state is separate from ledger truth."""
    __tablename__ = "finance_outbox"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(32), unique=True)
    event_type: Mapped[str] = mapped_column(String(64))
    source_id: Mapped[int] = mapped_column(Integer, ForeignKey("ledger_transactions.id"))
    account: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[float] = mapped_column(Float, default=time.time)
    lease_until: Mapped[float | None] = mapped_column(Float, nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    published_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str | None] = mapped_column(String(256), nullable=True)
    __table_args__ = (
        UniqueConstraint("event_type", "source_id", name="uq_finance_outbox_source"),
        Index("ix_finance_outbox_delivery", "status", "available_at", "id"),
    )


class ProcessedFinanceEvent(Base):
    __tablename__ = "processed_finance_events"
    consumer: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    payload_hash: Mapped[str] = mapped_column(String(64))
    processed_at: Mapped[float] = mapped_column(Float, default=time.time)


class ProjectedFinanceTransaction(Base):
    __tablename__ = "projected_finance_transactions"
    consumer: Mapped[str] = mapped_column(String(64), primary_key=True)
    ledger_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(32))
    source_hash: Mapped[str] = mapped_column(String(64))


class ProjectionAccount(Base):
    __tablename__ = "projection_accounts"
    consumer: Mapped[str] = mapped_column(String(64), primary_key=True)
    account: Mapped[str] = mapped_column(String(128), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)


class MonthlyFinanceSummary(Base):
    __tablename__ = "monthly_finance_summaries"
    account: Mapped[str] = mapped_column(String(128), primary_key=True)
    month: Mapped[str] = mapped_column(String(7), primary_key=True)
    gross_expense: Mapped[Decimal] = mapped_column(MoneyType())
    credits: Mapped[Decimal] = mapped_column(MoneyType())
    net_expense: Mapped[Decimal] = mapped_column(MoneyType())
    transaction_count: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)


class AnomalyTransaction(Base):
    __tablename__ = "anomaly_transactions"
    ledger_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account: Mapped[str] = mapped_column(String(128))
    txn_id: Mapped[str] = mapped_column(String(256))
    date: Mapped[date] = mapped_column(Date)
    merchant_key: Mapped[str] = mapped_column(String(512))
    amount: Mapped[Decimal] = mapped_column(MoneyType())
    __table_args__ = (Index("ix_anomaly_transactions_account_date", "account", "date"),)


class DuplicateChargeFinding(Base):
    __tablename__ = "duplicate_charge_findings"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account: Mapped[str] = mapped_column(String(128))
    merchant_key: Mapped[str] = mapped_column(String(512))
    amount: Mapped[Decimal] = mapped_column(MoneyType())
    txn_ids: Mapped[list] = mapped_column(JSON)
    dates: Mapped[list] = mapped_column(JSON)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)
    __table_args__ = (Index("ix_duplicate_charge_findings_account", "account"),)
