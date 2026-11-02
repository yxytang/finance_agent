"""Initial persistent ledger, document jobs and session event schema.

Revision ID: 20261026_01
Revises: None
"""
from alembic import op
import sqlalchemy as sa

revision = "20261026_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("sessions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("last_activity", sa.Float(), nullable=False),
        sa.Column("turns", sa.Integer(), nullable=False),
        sa.Column("busy", sa.Boolean(), nullable=False),
        sa.Column("messages", sa.JSON(), nullable=False),
        sa.Column("next_seq", sa.Integer(), nullable=False))
    op.create_table("session_events",
        sa.Column("session_id", sa.String(64), sa.ForeignKey("sessions.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("seq", sa.Integer(), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False))
    op.create_table("document_jobs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("root", sa.Text(), nullable=False),
        sa.Column("filename", sa.String(160), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("staged_path", sa.Text(), nullable=False),
        sa.Column("request_key", sa.String(128), nullable=False, unique=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.Float(), nullable=False),
        sa.Column("lease_until", sa.Float(), nullable=True),
        sa.Column("lease_token", sa.String(64), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False))
    op.create_index("ix_document_jobs_claim", "document_jobs", ["status", "available_at", "created_at"])
    op.create_table("import_batches",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("source_name", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("imported_rows", sa.Integer(), nullable=False),
        sa.Column("skipped_rows", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False))
    op.create_table("ledger_transactions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("txn_id", sa.String(256), nullable=False),
        sa.Column("account", sa.String(128), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("merchant", sa.String(512), nullable=False),
        sa.Column("merchant_key", sa.String(512), nullable=False),
        sa.Column("amount", sa.Numeric(20, 2).with_variant(sa.BigInteger(), "sqlite"), nullable=False),
        sa.Column("category", sa.String(64), nullable=True),
        sa.Column("batch_id", sa.String(32), sa.ForeignKey("import_batches.id"), nullable=False),
        sa.UniqueConstraint("account", "txn_id", name="uq_ledger_source_record"))
    op.create_index("ix_ledger_account_date", "ledger_transactions", ["account", "date"])
    op.create_index("ix_ledger_date_category", "ledger_transactions", ["date", "category"])
    op.create_index("ix_ledger_merchant_key", "ledger_transactions", ["merchant_key"])
    op.create_table("merchant_categories",
        sa.Column("merchant_key", sa.String(512), primary_key=True),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False))
    op.create_table("category_audits",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("merchant_key", sa.String(512), nullable=False),
        sa.Column("before_category", sa.String(64), nullable=True),
        sa.Column("after_category", sa.String(64), nullable=False),
        sa.Column("before_version", sa.Integer(), nullable=False),
        sa.Column("after_version", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(128), unique=True, nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False))
    op.create_index("ix_category_audit_merchant_time", "category_audits", ["merchant_key", "created_at"])


def downgrade():
    for name in ("category_audits", "merchant_categories", "ledger_transactions", "import_batches",
                 "document_jobs", "session_events", "sessions"):
        op.drop_table(name)
