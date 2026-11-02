"""Kafka protocol boundaries tested offline with explicit delivery/commit fakes."""

import json
import sys
from types import SimpleNamespace

import pytest

from backend.kafka_transport import (
    KafkaConsumerRunner, KafkaMessageError, KafkaProducerTransport,
    KafkaSettings, KafkaTransportError,
)


def event():
    return {"event_id": "e1", "account": "信用卡", "transaction": {"amount": "0.10"}}


class Producer:
    def __init__(self, *, error=None, pending=0, callback=True):
        self.error, self.pending, self.callback = error, pending, callback
        self.sent = []

    def produce(self, topic, **kwargs):
        self.sent.append((topic, kwargs))

    def flush(self, _timeout):
        if self.sent and self.callback:
            self.sent[-1][1]["on_delivery"](self.error, None)
        return self.pending


class Message:
    def __init__(self, value=None, key="信用卡".encode(), error=None):
        self._value = json.dumps(event(), ensure_ascii=False).encode() if value is None else value
        self._key, self._error = key, error

    def value(self): return self._value
    def key(self): return self._key
    def error(self): return self._error


class Consumer:
    def __init__(self, messages, *, order=None, commit_error=None, result=None):
        self.messages, self.order = iter(messages), order if order is not None else []
        self.commit_error, self.result = commit_error, result
        self.commits = []
        self.closed = False

    def subscribe(self, topics): self.topics = topics
    def poll(self, timeout): return next(self.messages, None)

    def commit(self, **kwargs):
        self.order.append("commit")
        self.commits.append(kwargs)
        if self.commit_error:
            raise self.commit_error
        return self.result

    def close(self): self.closed = True


class Service:
    def __init__(self, order, *, failure=None, result=True):
        self.order, self.failure, self.result = order, failure, result
        self.events = []

    def process(self, projection, message):
        self.order.append("database")
        self.events.append((projection, message))
        if self.failure:
            raise self.failure
        return self.result


def test_settings_support_security_and_hide_credentials():
    settings = KafkaSettings.from_env({"FINANCE_KAFKA_SECURITY_PROTOCOL": "SASL_SSL",
        "FINANCE_KAFKA_SASL_MECHANISM": "SCRAM-SHA-512",
        "FINANCE_KAFKA_SASL_USERNAME": "test-login", "FINANCE_KAFKA_SASL_PASSWORD": "do-not-log"})
    assert "do-not-log" not in repr(settings) and "test-login" not in repr(settings)
    assert settings.client_config()["sasl.password"] == "do-not-log"
    assert settings.group_id("summary") != settings.group_id("anomalies")


@pytest.mark.parametrize("values", [
    {"FINANCE_KAFKA_BOOTSTRAP_SERVERS": ""},
    {"FINANCE_KAFKA_BOOTSTRAP_SERVERS": "kafka://login:secret@localhost:9092"},
    {"FINANCE_KAFKA_TOPIC": "../bad/topic"},
    {"FINANCE_KAFKA_DELIVERY_TIMEOUT_MS": "NaN"},
    {"FINANCE_KAFKA_DELIVERY_TIMEOUT_MS": "0"},
    {"FINANCE_KAFKA_SECURITY_PROTOCOL": "SASL_SSL"},
])
def test_settings_reject_invalid_configuration(values):
    with pytest.raises(ValueError):
        KafkaSettings.from_env(values)


def test_publish_waits_for_delivery_and_preserves_decimal_strings():
    producer = Producer()
    transport = KafkaProducerTransport(KafkaSettings(), producer=producer)
    transport.publish(event())
    topic, sent = producer.sent[0]
    assert topic == "finance.ledger.v1" and sent["key"] == "信用卡".encode()
    assert json.loads(sent["value"])["transaction"]["amount"] == "0.10"
    assert b"\\u4fe1" not in sent["value"]
    transport.close()
    with pytest.raises(KafkaTransportError, match="closed"):
        transport.publish(event())


@pytest.mark.parametrize("producer", [Producer(error=RuntimeError("broker rejected")),
    Producer(callback=False), Producer(pending=1)])
def test_publish_never_confuses_queueing_with_acknowledged_delivery(producer):
    transport = KafkaProducerTransport(producer=producer)
    with pytest.raises(KafkaTransportError):
        transport.publish(event())


def test_publish_error_does_not_persist_credentials():
    class Broken(Producer):
        def produce(self, *_args, **_kwargs): raise RuntimeError("sasl.password=do-not-log")
    with pytest.raises(KafkaTransportError) as exc:
        KafkaProducerTransport(producer=Broken()).publish(event())
    assert "do-not-log" not in str(exc.value)


def test_consumer_commits_only_after_database_success_and_commits_deduplicated_event():
    order = []
    message = Message()
    consumer = Consumer([message], order=order)
    service = Service(order, result=False)
    runner = KafkaConsumerRunner(None, "summary", consumer=consumer, service=service)
    assert runner.poll_once()
    assert order == ["database", "commit"]
    assert consumer.commits == [{"message": message, "asynchronous": False}]
    assert service.events == [("summary", event())]
    runner.close()
    assert consumer.closed


def test_failed_database_transaction_cannot_be_skipped_by_subsequent_poll():
    order = []
    consumer = Consumer([Message(), Message()], order=order)
    runner = KafkaConsumerRunner(None, "anomalies", consumer=consumer,
        service=Service(order, failure=RuntimeError("database unavailable")))
    with pytest.raises(RuntimeError, match="database unavailable"):
        runner.poll_once()
    assert not consumer.commits
    with pytest.raises(KafkaTransportError, match="restart"):
        runner.poll_once()
    assert order == ["database"]


@pytest.mark.parametrize("message", [Message(value=b"{bad"), Message(value=b"\xff"),
    Message(value=b"[]"), Message(key=b"another-account"), Message(error=RuntimeError("broker error"))])
def test_bad_message_does_not_commit_or_process(message):
    order = []
    consumer = Consumer([message], order=order)
    service = Service(order)
    runner = KafkaConsumerRunner(None, "summary", consumer=consumer, service=service)
    with pytest.raises((KafkaMessageError, KafkaTransportError)):
        runner.poll_once()
    assert not consumer.commits and not service.events


@pytest.mark.parametrize("failure,result", [(RuntimeError("rebalance"), None),
    (None, [SimpleNamespace(err=RuntimeError("partition commit failed"))])])
def test_commit_failure_stops_runner_after_persisted_database_transaction(failure, result):
    order = []
    consumer = Consumer([Message()], order=order, commit_error=failure, result=result)
    service = Service(order)
    runner = KafkaConsumerRunner(None, "summary", consumer=consumer, service=service)
    with pytest.raises((RuntimeError, KafkaTransportError)):
        runner.poll_once()
    assert order == ["database", "commit"] and len(service.events) == 1
    with pytest.raises(KafkaTransportError, match="restart"):
        runner.poll_once()


def test_empty_poll_does_not_update_database_or_offset():
    order = []
    consumer = Consumer([], order=order)
    runner = KafkaConsumerRunner(None, "summary", consumer=consumer, service=Service(order))
    assert runner.poll_once() is False
    assert not order


def test_real_client_configuration_disables_unacknowledged_offset_advancement(monkeypatch):
    configs = {}
    consumer = Consumer([])
    producer = Producer()

    def make_consumer(config):
        configs["consumer"] = config
        return consumer

    def make_producer(config):
        configs["producer"] = config
        return producer

    monkeypatch.setitem(sys.modules, "confluent_kafka", SimpleNamespace(
        Consumer=make_consumer, Producer=make_producer))
    KafkaProducerTransport()
    KafkaConsumerRunner(None, "summary", service=Service([]))
    assert configs["producer"]["enable.idempotence"] is True
    assert configs["producer"]["acks"] == "all"
    assert configs["consumer"]["enable.auto.commit"] is False
    assert configs["consumer"]["enable.auto.offset.store"] is False
    assert configs["consumer"]["group.id"] == "finance.summary.v1"


@pytest.fixture
def cli(monkeypatch):
    import dotenv
    from backend import kafka_worker
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(kafka_worker.KafkaSettings, "from_env", lambda: KafkaSettings())
    database = SimpleNamespace(create_schema=lambda: None, disposed=False)
    database.dispose = lambda: setattr(database, "disposed", True)
    monkeypatch.setattr(kafka_worker, "Database", lambda: database)
    return kafka_worker, database


def test_backfill_cli_works_without_kafka_dependency(cli, monkeypatch, capsys):
    worker, db = cli
    monkeypatch.setitem(sys.modules, "backend.outbox", SimpleNamespace(backfill_events=lambda actual: 3))
    assert worker.main(["backfill"]) == 0
    assert db.disposed
    assert "3" in capsys.readouterr().out


def test_publisher_cli_once_releases_resources(cli, monkeypatch):
    worker, db = cli
    client = SimpleNamespace(closed=False)
    client.close = lambda: setattr(client, "closed", True)
    monkeypatch.setattr(worker, "KafkaProducerTransport", lambda settings: client)
    monkeypatch.setitem(sys.modules, "backend.outbox", SimpleNamespace(
        OutboxPublisher=lambda actual_db, actual_client, **kwargs: SimpleNamespace(publish_once=lambda: True)))
    assert worker.main(["publish", "--once"]) == 0
    assert db.disposed and client.closed


def test_consumer_cli_failure_is_nonzero_and_does_not_print_private_error(cli, monkeypatch, capsys):
    worker, db = cli
    client = SimpleNamespace(closed=False)
    client.close = lambda: setattr(client, "closed", True)

    def fail(**_kwargs):
        raise RuntimeError("sasl.password=do-not-log; private financial transaction")

    client.poll_once = fail
    monkeypatch.setattr(worker, "KafkaConsumerRunner", lambda *_args, **_kwargs: client)
    assert worker.main(["consume", "--projection", "summary", "--once"]) == 1
    assert db.disposed and client.closed
    assert "do-not-log" not in capsys.readouterr().err


def test_consumer_cli_empty_once_has_bounded_timeout(cli, monkeypatch):
    worker, db = cli
    client = SimpleNamespace(poll_once=lambda **_: False, close=lambda: None)
    monkeypatch.setattr(worker, "KafkaConsumerRunner", lambda *_args, **_kwargs: client)
    clock = iter([0.0, 0.0, 1.0])
    monkeypatch.setattr(worker.time, "monotonic", lambda: next(clock))
    assert worker.main(["consume", "--projection", "summary", "--once", "--timeout-seconds", "0.1"]) == 2
    assert db.disposed
