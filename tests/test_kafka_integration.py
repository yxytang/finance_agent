"""Optional real Kafka protocol tests; each run uses a unique topic and DB."""
from datetime import date
from decimal import Decimal
import os
import sys
import time
import uuid

import pytest
from sqlalchemy import select

from backend.ledger import LedgerRepository
from backend.models import FinanceOutbox
from backend.outbox import OutboxPublisher
from backend.projections import ProjectionService
from backend.kafka_transport import KafkaConsumerRunner, KafkaProducerTransport, KafkaSettings, init_topics
from fa.models import Transaction
from tests.test_backend_ledger import db

pytestmark = pytest.mark.skipif(not os.environ.get("FINANCE_TEST_KAFKA_BOOTSTRAP_SERVERS"),
                                reason="FINANCE_TEST_KAFKA_BOOTSTRAP_SERVERS not set")


@pytest.fixture
def kafka_settings():
    pytest.importorskip("confluent_kafka")
    settings = KafkaSettings(bootstrap_servers=os.environ["FINANCE_TEST_KAFKA_BOOTSTRAP_SERVERS"],
                             topic="finance-test-" + uuid.uuid4().hex, delivery_timeout_ms=5000)
    init_topics(settings)
    yield settings
    # Native Windows Kafka cannot reliably rename open log segments during
    # DeleteTopics. These unique topics live only on the disposable test broker;
    # Linux CI cleans them normally. Never take down its other test topics.
    if sys.platform == "win32":
        return
    from confluent_kafka.admin import AdminClient
    admin = AdminClient(settings.client_config())
    future = admin.delete_topics([settings.topic])[settings.topic]
    future.result(timeout=10)


def test_real_broker_outbox_redelivery_and_commit_failure_recover(db, kafka_settings):
    from confluent_kafka import Consumer
    ledger = LedgerRepository(db)
    ledger.import_transactions([
        Transaction(date(2026, 10, 1), "Shop", Decimal("0.10"), "card", "a", "购物"),
        Transaction(date(2026, 10, 2), "Shop", Decimal("0.10"), "card", "b", "购物"),
        Transaction(date(2026, 10, 2), "Shop", Decimal("-0.03"), "card", "refund", "购物"),
    ], source_name="kafka-integration")
    producer = KafkaProducerTransport(kafka_settings)
    try:
        publisher = OutboxPublisher(db, producer)
        while publisher.publish_once():
            pass
        with db.session() as session:
            events = session.scalars(select(FinanceOutbox).order_by(FinanceOutbox.id)).all()
            assert all(row.status == "published" for row in events)
        producer.publish(events[0].payload)  # A real broker redelivery, same event ID.
    finally:
        producer.close()

    # Simulate death after the DB transaction commits but before the Kafka
    # offset commit. Restart must re-read and safely deduplicate that record.
    group = kafka_settings.group_id("summary")
    raw = Consumer({**kafka_settings.client_config(), "group.id": group,
                    "enable.auto.commit": False, "enable.auto.offset.store": False,
                    "auto.offset.reset": "earliest"})
    class FailCommit:
        def subscribe(self, topics): raw.subscribe(topics)
        def poll(self, timeout): return raw.poll(timeout)
        def commit(self, **_): raise RuntimeError("crash before offset commit")
        def close(self): raw.close()
    first = KafkaConsumerRunner(db, "summary", kafka_settings, consumer=FailCommit())
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                first.poll_once(1)
            except RuntimeError as exc:
                assert "crash before offset" in str(exc)
                break
        else:
            pytest.fail("real Kafka message did not arrive")
    finally:
        first.close()
    for projection in ("summary", "anomalies"):
        runner = KafkaConsumerRunner(db, projection, kafka_settings)
        try:
            consumed = 0
            deadline = time.monotonic() + 30
            while consumed < 4 and time.monotonic() < deadline:
                consumed += runner.poll_once(1)
            assert consumed == 4
        finally:
            runner.close()
    service = ProjectionService(db)
    assert service.summary()[0]["net_expense"] == "0.17"
    assert service.summary()[0]["transaction_count"] == 3
    assert service.anomalies()[0]["txn_ids"] == ["a", "b"]


def test_unreachable_broker_does_not_lose_committed_outbox(db):
    LedgerRepository(db).import_transactions([
        Transaction(date(2026, 10, 1), "Shop", Decimal("1.00"), "card", "a")
    ], source_name="broker-outage")
    producer = KafkaProducerTransport(KafkaSettings(bootstrap_servers="127.0.0.1:1", delivery_timeout_ms=1000))
    try:
        assert OutboxPublisher(db, producer).publish_once()
    finally:
        producer.close()
    with db.session() as session:
        row = session.scalar(select(FinanceOutbox))
        assert row.status == "pending" and row.attempts == 1
    assert len(LedgerRepository(db).list_transactions()) == 1
