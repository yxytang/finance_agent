"""Offline financial projection transactions; PostgreSQL is opt-in."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import os
import threading
import uuid

import pytest
from sqlalchemy import delete, event as sql_event, func, select, text

from backend.db import Database
from backend.models import (
    AnomalyTransaction, DuplicateChargeFinding, MonthlyFinanceSummary, ProcessedFinanceEvent,
    ProjectedFinanceTransaction, ProjectionAccount,
)
from backend.projections import (
    InvalidFinanceEvent, ProjectionAmountOverflow, ProjectionService, validate_event,
)
from fa.normalize import normalize


@pytest.fixture(params=["sqlite", "postgresql"])
def db(request, tmp_path):
    schema = None
    if request.param == "postgresql":
        url = os.environ.get("FINANCE_TEST_DATABASE_URL")
        if not url:
            pytest.skip("FINANCE_TEST_DATABASE_URL not set")
        database = Database(url)
        schema = "projection_test_" + uuid.uuid4().hex
        with database.engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        database.engine.dispose()

        @sql_event.listens_for(database.engine, "connect")
        def set_schema(connection, _):
            cursor = connection.cursor()
            cursor.execute(f'SET search_path TO "{schema}"')
            connection.commit()
            cursor.close()
    else:
        database = Database(f"sqlite:///{(tmp_path / 'projections.sqlite3').as_posix()}")
    database.create_schema()
    try:
        yield database
    finally:
        database.dispose()
        if schema:
            cleanup = Database(url)
            with cleanup.engine.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            cleanup.dispose()


def financial_event(ledger_id=1, amount="10.00", *, account="card", happened="2026-10-01",
                    merchant="TARGET #123", txn_id=None):
    return {
        "event_id": uuid.uuid4().hex, "event_type": "ledger.transaction_imported.v1",
        "schema_version": 1, "account": account, "occurred_at": 1790784000.0,
        "transaction": {
            "id": ledger_id, "txn_id": txn_id or f"txn-{ledger_id}", "date": happened,
            "merchant": merchant, "merchant_key": normalize(merchant), "amount": amount,
            "category": "购物", "batch_id": "a" * 32,
        },
    }


def test_summary_is_exact_and_receipts_are_consumer_scoped(db):
    service = ProjectionService(db)
    events = [financial_event(1, "0.10"), financial_event(2, "0.20"),
              financial_event(3, "-0.01"), financial_event(4, "0.00")]
    for item in events:
        assert service.process("summary", item)
        assert not service.process("summary", item)
        assert service.process("anomalies", item)
    row = service.summary("card")[0]
    assert {key: row[key] for key in (
        "gross_expense", "credits", "net_expense", "transaction_count",
    )} == {"gross_expense": "0.30", "credits": "0.01", "net_expense": "0.29",
           "transaction_count": 4}
    assert row["month"] == "2026-10" and row["updated_at"] > 0
    assert service.summary("unknown") == []


def test_business_identity_dedup_survives_a_new_event_id(db):
    service = ProjectionService(db)
    original = financial_event()
    assert service.process("summary", original)
    alias = deepcopy(original)
    alias["event_id"] = uuid.uuid4().hex
    alias["occurred_at"] += 1
    assert validate_event(alias).source_hash == validate_event(original).source_hash
    assert validate_event(alias).payload_hash != validate_event(original).payload_hash
    assert not service.process("summary", alias)
    assert not service.process("summary", alias)
    assert service.summary()[0]["transaction_count"] == 1
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(ProcessedFinanceEvent)) == 2
        assert session.scalar(select(func.count()).select_from(ProjectedFinanceTransaction)) == 1


@pytest.mark.parametrize("reuse_id", [True, False])
def test_conflicting_event_or_source_identity_is_rejected_atomically(db, reuse_id):
    service = ProjectionService(db)
    original = financial_event()
    service.process("summary", original)
    changed = deepcopy(original)
    if not reuse_id:
        changed["event_id"] = uuid.uuid4().hex
    changed["transaction"]["amount"] = "99.00"
    with pytest.raises(InvalidFinanceEvent, match="reused"):
        service.process("summary", changed)
    assert service.summary()[0]["net_expense"] == "10.00"
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(ProcessedFinanceEvent)) == 1


def test_projection_failure_rolls_back_receipts_and_can_retry(db):
    service = ProjectionService(db)
    item = financial_event()

    def reject(_connection, _cursor, statement, *_):
        if statement.lstrip().lower().startswith("insert into " + MonthlyFinanceSummary.__tablename__):
            raise RuntimeError("simulated summary write failure")

    sql_event.listen(db.engine, "before_cursor_execute", reject)
    try:
        with pytest.raises(RuntimeError, match="summary write failure"):
            service.process("summary", item)
    finally:
        sql_event.remove(db.engine, "before_cursor_execute", reject)
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(ProcessedFinanceEvent)) == 0
        assert session.scalar(select(func.count()).select_from(ProjectedFinanceTransaction)) == 0
    assert service.summary() == []
    assert service.process("summary", item)


def test_concurrent_events_do_not_lose_totals(db):
    service = ProjectionService(db)
    barrier = threading.Barrier(8)

    def apply(number):
        item = financial_event(number, "0.10")
        barrier.wait()
        return service.process("summary", item)

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(pool.map(apply, range(1, 9)))
    assert service.summary()[0]["net_expense"] == "0.80"
    assert service.summary()[0]["transaction_count"] == 8


def test_concurrent_redelivery_is_applied_once(db):
    service = ProjectionService(db)
    item = financial_event()
    barrier = threading.Barrier(4)

    def apply(_):
        barrier.wait()
        return service.process("summary", item)

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(apply, range(4))).count(True) == 1
    assert service.summary()[0]["transaction_count"] == 1


def test_month_and_account_totals_are_independent(db):
    service = ProjectionService(db)
    for item in [financial_event(1), financial_event(2, happened="2026-11-01"),
                 financial_event(3, account="savings", amount="-25.00")]:
        service.process("summary", item)
    assert [(r["account"], r["month"], r["net_expense"]) for r in service.summary()] == [
        ("card", "2026-10", "10.00"), ("card", "2026-11", "10.00"),
        ("savings", "2026-10", "-25.00"),
    ]


def test_large_aggregate_is_rejected_without_silent_real_promotion(db):
    service = ProjectionService(db)
    first = financial_event(1, "50000000000000000.01")
    second = financial_event(2, "50000000000000000.01")
    assert service.process("summary", first)
    before = service.summary()
    with pytest.raises(ProjectionAmountOverflow, match="aggregate exceeds"):
        service.process("summary", second)
    assert service.summary() == before
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(ProcessedFinanceEvent)) == 1
        assert session.scalar(select(func.count()).select_from(ProjectedFinanceTransaction)) == 1
        if db.engine.dialect.name == "sqlite":
            assert session.execute(text(
                "SELECT typeof(gross_expense), typeof(credits), typeof(net_expense) "
                "FROM monthly_finance_summaries"
            )).one() == ("integer", "integer", "integer")


def test_aggregate_preserves_cents_up_to_storage_boundary(db):
    service = ProjectionService(db)
    if db.engine.dialect.name == "sqlite":
        near = "92233720368547758.06"
        expected = "92233720368547758.07"  # INT64_MAX cents
    else:
        near = "99999999999999999.98"
        expected = "99999999999999999.99"  # MoneyType's enforced limit
    service.process("summary", financial_event(1, near))
    service.process("summary", financial_event(2, "0.01"))
    assert service.summary()[0]["gross_expense"] == expected
    assert service.summary()[0]["net_expense"] == expected
    with pytest.raises(ProjectionAmountOverflow):
        service.process("summary", financial_event(3, "0.01"))
    assert service.summary()[0]["gross_expense"] == expected
    assert service.summary()[0]["transaction_count"] == 2


def test_duplicate_charges_are_account_scoped_and_positive_only(db):
    service = ProjectionService(db)
    for item in [financial_event(1), financial_event(2, account="other"),
                 financial_event(3, "-10.00"), financial_event(4, "-10.00")]:
        service.process("anomalies", item)
    assert service.anomalies() == []
    item = financial_event(5, happened="2026-10-03", merchant="TARGET #999")
    assert service.process("anomalies", item)
    assert not service.process("anomalies", item)
    result = service.anomalies("card")
    assert len(result) == 1
    assert result[0]["txn_ids"] == ["txn-1", "txn-5"]
    assert result[0]["amount"] == "10.00"
    assert result[0]["kind"] == "suspected_duplicate_charge"
    assert result[0]["window_days"] == 3
    assert service.anomalies("other") == []


def test_out_of_order_events_merge_duplicate_clusters(db):
    service = ProjectionService(db)
    for item in [financial_event(1, happened="2026-10-01"),
                 financial_event(3, happened="2026-10-07")]:
        service.process("anomalies", item)
    assert service.anomalies() == []
    service.process("anomalies", financial_event(2, happened="2026-10-04"))
    result = service.anomalies()[0]
    assert result["txn_ids"] == ["txn-1", "txn-2", "txn-3"]
    assert result["dates"] == ["2026-10-01", "2026-10-04", "2026-10-07"]


def test_anomaly_failure_rolls_back_fact_receipt_and_previous_findings(db):
    service = ProjectionService(db)
    service.process("anomalies", financial_event(1))
    service.process("anomalies", financial_event(2))
    before = service.anomalies()
    item = financial_event(3)

    def reject(*_):
        raise RuntimeError("simulated finding failure")

    sql_event.listen(DuplicateChargeFinding, "before_insert", reject)
    try:
        with pytest.raises(RuntimeError, match="finding failure"):
            service.process("anomalies", item)
    finally:
        sql_event.remove(DuplicateChargeFinding, "before_insert", reject)
    assert service.anomalies() == before
    assert service.process("anomalies", item)
    assert service.anomalies()[0]["occurrences"] == 3


def test_fresh_projection_replay_rebuilds_same_financial_evidence(db):
    events = [financial_event(1, "0.10"), financial_event(2, "0.10"),
              financial_event(3, "-0.03")]
    service = ProjectionService(db)
    for item in events:
        service.process("summary", item)
        service.process("anomalies", item)
    before_summary = service.summary()
    before_anomalies = service.anomalies()
    for item in reversed(events):
        assert not service.process("summary", item)
        assert not service.process("anomalies", item)
    assert service.summary() == before_summary
    assert service.anomalies() == before_anomalies
    # Rebuild an empty derived store from the retained events in another order.
    # Only these isolated test projections are cleared; the ledger is untouched.
    with db.session() as session:
        for model in (DuplicateChargeFinding, AnomalyTransaction, MonthlyFinanceSummary,
                      ProjectedFinanceTransaction, ProcessedFinanceEvent, ProjectionAccount):
            session.execute(delete(model))
    for item in reversed(events):
        assert service.process("summary", item)
        assert service.process("anomalies", item)
    without_time = lambda rows: [{key: value for key, value in row.items() if key != "updated_at"}
                                 for row in rows]
    assert without_time(service.summary()) == without_time(before_summary)
    assert without_time(service.anomalies()) == without_time(before_anomalies)


@pytest.mark.parametrize("change", [
    {"schema_version": 2}, {"schema_version": True}, {"account": ""},
    {"event_type": "unknown"}, {"occurred_at": float("nan")}, {"occurred_at": 10**400},
    {"event_id": "not-an-event-id"}, {"transaction": None},
])
def test_invalid_envelopes_fail_before_any_database_write(db, change):
    item = financial_event()
    item.update(change)
    with pytest.raises(InvalidFinanceEvent):
        ProjectionService(db).process("summary", item)
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(ProcessedFinanceEvent)) == 0


@pytest.mark.parametrize("field,value", [
    ("amount", 0.1), ("amount", "NaN"), ("amount", "1.001"),
    ("amount", "100000000000000000"), ("date", "2026-02-30"),
    ("date", "20261001"), ("id", True), ("merchant_key", "inconsistent"),
])
def test_invalid_source_data_is_not_silently_coerced(db, field, value):
    item = financial_event()
    item["transaction"][field] = value
    with pytest.raises(InvalidFinanceEvent):
        ProjectionService(db).process("summary", item)
    assert ProjectionService(db).summary() == []
