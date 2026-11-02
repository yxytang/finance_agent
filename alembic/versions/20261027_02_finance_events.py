"""Transactional outbox and idempotent financial projections.

Revision ID: 20261027_02
Revises: 20261026_01
"""
from alembic import op
import sqlalchemy as sa

revision = "20261027_02"
down_revision = "20261026_01"
branch_labels = None
depends_on = None


def money():
    return sa.Numeric(20, 2).with_variant(sa.BigInteger(), "sqlite")


def upgrade():
    op.create_table("finance_outbox",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String(32), nullable=False, unique=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("ledger_transactions.id"), nullable=False),
        sa.Column("account", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.Float(), nullable=False),
        sa.Column("lease_until", sa.Float(), nullable=True),
        sa.Column("lease_token", sa.String(64), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("published_at", sa.Float(), nullable=True),
        sa.Column("error", sa.String(256), nullable=True),
        sa.UniqueConstraint("event_type", "source_id", name="uq_finance_outbox_source"))
    op.create_index("ix_finance_outbox_delivery", "finance_outbox", ["status", "available_at", "id"])
    op.create_table("processed_finance_events",
        sa.Column("consumer", sa.String(64), primary_key=True),
        sa.Column("event_id", sa.String(32), primary_key=True),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("processed_at", sa.Float(), nullable=False))
    op.create_table("projected_finance_transactions",
        sa.Column("consumer", sa.String(64), primary_key=True),
        sa.Column("ledger_id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.String(32), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False))
    op.create_table("projection_accounts",
        sa.Column("consumer", sa.String(64), primary_key=True),
        sa.Column("account", sa.String(128), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False))
    op.create_table("monthly_finance_summaries",
        sa.Column("account", sa.String(128), primary_key=True),
        sa.Column("month", sa.String(7), primary_key=True),
        sa.Column("gross_expense", money(), nullable=False),
        sa.Column("credits", money(), nullable=False),
        sa.Column("net_expense", money(), nullable=False),
        sa.Column("transaction_count", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False))
    op.create_table("anomaly_transactions",
        sa.Column("ledger_id", sa.Integer(), primary_key=True),
        sa.Column("account", sa.String(128), nullable=False),
        sa.Column("txn_id", sa.String(256), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("merchant_key", sa.String(512), nullable=False),
        sa.Column("amount", money(), nullable=False))
    op.create_index("ix_anomaly_transactions_account_date", "anomaly_transactions", ["account", "date"])
    op.create_table("duplicate_charge_findings",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("account", sa.String(128), nullable=False),
        sa.Column("merchant_key", sa.String(512), nullable=False),
        sa.Column("amount", money(), nullable=False),
        sa.Column("txn_ids", sa.JSON(), nullable=False),
        sa.Column("dates", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False))
    op.create_index("ix_duplicate_charge_findings_account", "duplicate_charge_findings", ["account"])


def downgrade():
    for table in ("duplicate_charge_findings", "anomaly_transactions", "monthly_finance_summaries",
                  "projection_accounts", "projected_finance_transactions", "processed_finance_events",
                  "finance_outbox"):
        op.drop_table(table)
