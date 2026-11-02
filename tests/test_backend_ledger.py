"""Offline integration tests; optional isolated PostgreSQL schema via test URL."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
import os
from pathlib import Path
import threading
import uuid

import pytest
from sqlalchemy import event, func, inspect, select, text

from backend.db import Database
from backend.ledger import LedgerConflict, LedgerRepository, LedgerValidationError
from backend.models import Base, CategoryAudit, ImportBatch, MerchantCategory
from fa.models import Transaction


@pytest.fixture(params=["sqlite", "postgresql"])
def db(request, tmp_path):
    schema = None
    if request.param == "postgresql":
        url = os.environ.get("FINANCE_TEST_DATABASE_URL")
        if not url:
            pytest.skip("FINANCE_TEST_DATABASE_URL not set")
        database = Database(url)
        schema = "backend_test_" + uuid.uuid4().hex
        with database.engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        database.engine.dispose()

        @event.listens_for(database.engine, "connect")
        def set_schema(connection, _):
            cursor = connection.cursor()
            cursor.execute(f'SET search_path TO "{schema}"')
            connection.commit()
            cursor.close()
    else:
        database = Database(f"sqlite:///{(tmp_path / 'ledger.sqlite3').as_posix()}")
    database.create_schema()
    try:
        yield database
    finally:
        database.dispose()
        if schema:
            cleanup = Database(url)
            with cleanup.engine.begin() as conn:
                conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            cleanup.dispose()


def txn(txn_id="t1", amount="10.01", *, account="checking", merchant="TARGET #123", category="购物"):
    return Transaction(date(2026, 10, 1), merchant, Decimal(amount), account, txn_id, category)


def seed(db):
    repo = LedgerRepository(db)
    repo.import_transactions([txn()], source_name="test")
    return repo


def test_import_idempotency_does_not_hide_double_charge(db):
    repo = LedgerRepository(db)
    original = [txn("a"), txn("b"), txn("a", account="credit")]
    result = repo.import_transactions(original, source_name="first")
    assert result["imported_rows"] == 3
    assert repo.import_transactions(original, source_name="again")["repeated"]
    overlap = repo.import_transactions([txn("a"), txn("new")], source_name="overlap")
    assert (overlap["imported_rows"], overlap["skipped_rows"]) == (1, 1)
    assert len(repo.list_transactions()) == 4


def test_changed_source_rolls_back_entire_import(db):
    repo = seed(db)
    with pytest.raises(LedgerConflict):
        repo.import_transactions([txn("new"), txn(amount="999.00")], source_name="bad")
    assert [t.txn_id for t in repo.list_transactions()] == ["t1"]
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(ImportBatch)) == 1


def test_exact_money_and_negative_refund_survive_restart(db):
    repo = LedgerRepository(db)
    repo.import_transactions([txn("a", "0.10"), txn("b", "0.20"), txn("refund", "-0.01")], source_name="money")
    db.dispose()  # Force fresh DB connections; data must survive the old pool.
    assert sum(t.amount for t in LedgerRepository(db).list_transactions()) == Decimal("0.29")
    if db.engine.dialect.name == "sqlite":
        with db.engine.connect() as connection:
            assert connection.execute(text("SELECT amount FROM ledger_transactions WHERE txn_id='a'")).scalar() == 10


@pytest.mark.parametrize("bad_amount", ["NaN", "Infinity", "1.001", "100000000000000000"])
def test_invalid_money_rejected_without_partial_write(db, bad_amount):
    repo = LedgerRepository(db)
    with pytest.raises(LedgerValidationError):
        repo.import_transactions([txn("ok"), txn("bad", bad_amount)], source_name="invalid")
    assert repo.list_transactions() == ()


def test_category_version_idempotency_and_new_import_override(db):
    repo = seed(db)
    assert repo.category_state("TARGET #123")["version"] == 0
    changed = repo.correct_category("TARGET #123", "超市", expected_version=0, request_id="request-one")
    assert changed["version"] == 1
    assert repo.correct_category("TARGET #123", "超市", expected_version=0, request_id="request-one") == changed
    with pytest.raises(LedgerConflict):
        repo.correct_category("TARGET #123", "订阅", expected_version=0)
    with pytest.raises(LedgerConflict):
        repo.correct_category("TARGET #123", "订阅", request_id="request-one")
    repo.import_transactions([txn("next")], source_name="future")
    assert {t.category for t in repo.list_transactions()} == {"超市"}
    assert not repo.correct_category("TARGET #123", "超市", expected_version=1)["changed"]
    with db.session() as session:
        audits = session.scalars(select(CategoryAudit)).all()
        assert len(audits) == 1
        assert (audits[0].before_category, audits[0].after_category) == ("购物", "超市")


def test_audit_failure_rolls_back_category(db):
    repo = seed(db)

    def reject(*_):
        raise RuntimeError("simulated audit failure")

    event.listen(CategoryAudit, "before_insert", reject)
    try:
        with pytest.raises(RuntimeError, match="audit failure"):
            repo.correct_category("TARGET #123", "超市", expected_version=0)
    finally:
        event.remove(CategoryAudit, "before_insert", reject)
    assert repo.category_state("TARGET #123") == {"merchant": "target", "category": "购物", "version": 0}
    assert repo.list_transactions()[0].category == "购物"


def test_concurrent_category_update_cannot_lose_a_confirmed_write(db):
    repo = seed(db)
    repo.correct_category("target", "超市", expected_version=0)
    barrier = threading.Barrier(2)

    def change(category):
        barrier.wait()
        try:
            return repo.correct_category("target", category, expected_version=1)
        except LedgerConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(change, ["订阅", "娱乐"]))
    assert results.count("conflict") == 1
    assert repo.category_state("target")["version"] == 2
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(CategoryAudit)) == 2


def test_concurrent_identical_import_is_applied_once(db):
    repo = LedgerRepository(db)
    barrier = threading.Barrier(2)

    def import_same(_):
        barrier.wait()
        return repo.import_transactions([txn("a"), txn("b")], source_name="simultaneous")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(import_same, range(2)))
    assert sorted(r["imported_rows"] for r in results) == [0, 2]
    assert len(repo.list_transactions()) == 2
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(ImportBatch)) == 1


def test_concurrent_first_correction_with_same_request_replays_once(db):
    repo = seed(db)
    barrier = threading.Barrier(2)

    def correct_same(_):
        barrier.wait()
        return repo.correct_category("target", "超市", expected_version=0, request_id="same-click")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(correct_same, range(2)))
    assert results[0] == results[1]
    assert repo.category_state("target")["version"] == 1
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(CategoryAudit)) == 1


def test_csv_can_preserve_offline_classification(db, tmp_path):
    path = tmp_path / "bill.csv"
    path.write_text("txn_id,date,merchant,amount,account\na,2026-10-01,TARGET #123,10.01,checking\n", encoding="utf-8")
    repo = LedgerRepository(db)
    tagged = [txn("a", category="超市")]
    assert repo.import_csv(path, transactions=tagged)["imported_rows"] == 1
    assert repo.list_transactions()[0].category == "超市"
    assert repo.import_csv(path)["repeated"]
    with pytest.raises(LedgerValidationError):
        repo.import_csv(path, transactions=[txn("changed")])


def test_migration_upgrade_matches_models_and_downgrade(db):
    from alembic import command
    from alembic.config import Config
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    Base.metadata.drop_all(db.engine)
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with db.engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
        assert set(Base.metadata.tables) <= set(inspect(connection).get_table_names())
        assert compare_metadata(MigrationContext.configure(connection, opts={"compare_type": True}), Base.metadata) == []
        command.downgrade(config, "base")
        assert not set(Base.metadata.tables) & set(inspect(connection).get_table_names())
        command.upgrade(config, "head")
    assert LedgerRepository(db).list_transactions() == ()
