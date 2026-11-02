"""Optional Kafka adapters; ordinary API startup never creates a Kafka client.

Producer delivery reports are awaited before the Outbox is acknowledged.
Consumers commit only the individual message whose database transaction has
completed. A malformed event or failed transaction stops this runner; callers
must not continue to a later offset after such a failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import re
import threading
from typing import Mapping


MAX_EVENT_BYTES = 64 * 1024
PROJECTIONS = frozenset({"summary", "anomalies"})


class KafkaTransportError(RuntimeError):
    """A bounded Kafka operation failed; its outcome may be uncertain."""


class KafkaMessageError(ValueError):
    """A message cannot safely be consumed. Its offset is not committed."""


def _name(value: str, label: str) -> str:
    if not value or len(value) > 249 or value in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9._-]+", value):
        raise ValueError(f"Invalid Kafka {label}")
    return value


@dataclass(frozen=True)
class KafkaSettings:
    bootstrap_servers: str = "localhost:9092"
    topic: str = "finance.ledger.v1"
    group_prefix: str = "finance"
    delivery_timeout_ms: int = 10000
    security_protocol: str = "PLAINTEXT"
    sasl_mechanism: str | None = None
    sasl_username: str | None = field(default=None, repr=False)
    sasl_password: str | None = field(default=None, repr=False)
    ssl_ca_location: str | None = None

    def __post_init__(self):
        if (not self.bootstrap_servers.strip() or "://" in self.bootstrap_servers
                or any(c.isspace() for c in self.bootstrap_servers)):
            raise ValueError("Invalid Kafka bootstrap servers; use host:port entries")
        _name(self.topic, "topic")
        _name(self.group_prefix, "group prefix")
        if not 1000 <= self.delivery_timeout_ms <= 300000:
            raise ValueError("Kafka delivery timeout must be 1000..300000 milliseconds")
        if self.security_protocol not in {"PLAINTEXT", "SSL", "SASL_PLAINTEXT", "SASL_SSL"}:
            raise ValueError("Invalid Kafka security protocol")
        if self.security_protocol.startswith("SASL"):
            if self.sasl_mechanism not in {"PLAIN", "SCRAM-SHA-256", "SCRAM-SHA-512"}:
                raise ValueError("Kafka SASL requires PLAIN or SCRAM mechanism")
            if not self.sasl_username or not self.sasl_password:
                raise ValueError("Kafka SASL requires username and password")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "KafkaSettings":
        env = os.environ if environ is None else environ
        try:
            timeout = int(env.get("FINANCE_KAFKA_DELIVERY_TIMEOUT_MS", "10000"))
        except ValueError:
            raise ValueError("Kafka delivery timeout must be an integer") from None
        return cls(
            bootstrap_servers=env.get("FINANCE_KAFKA_BOOTSTRAP_SERVERS", "localhost:9092").strip(),
            topic=env.get("FINANCE_KAFKA_TOPIC", "finance.ledger.v1").strip(),
            group_prefix=env.get("FINANCE_KAFKA_GROUP_PREFIX", "finance").strip(),
            delivery_timeout_ms=timeout,
            security_protocol=env.get("FINANCE_KAFKA_SECURITY_PROTOCOL", "PLAINTEXT").strip().upper(),
            sasl_mechanism=env.get("FINANCE_KAFKA_SASL_MECHANISM", "").strip().upper() or None,
            sasl_username=env.get("FINANCE_KAFKA_SASL_USERNAME") or None,
            sasl_password=env.get("FINANCE_KAFKA_SASL_PASSWORD") or None,
            ssl_ca_location=env.get("FINANCE_KAFKA_SSL_CA_LOCATION", "").strip() or None,
        )

    def client_config(self) -> dict:
        config = {"bootstrap.servers": self.bootstrap_servers,
                  "security.protocol": self.security_protocol,
                  "socket.timeout.ms": self.delivery_timeout_ms}
        if self.security_protocol.startswith("SASL"):
            config.update({"sasl.mechanism": self.sasl_mechanism,
                           "sasl.username": self.sasl_username,
                           "sasl.password": self.sasl_password})
        if self.ssl_ca_location:
            config["ssl.ca.location"] = self.ssl_ca_location
        return config

    def group_id(self, projection: str) -> str:
        if projection not in PROJECTIONS:
            raise ValueError("Unknown financial projection")
        return _name(f"{self.group_prefix}.{projection}.v1", "consumer group")


def _kafka():
    try:
        import confluent_kafka
    except ImportError:
        raise RuntimeError('Kafka client is missing; install finance-agent[web,kafka]') from None
    return confluent_kafka


class KafkaProducerTransport:
    def __init__(self, settings: KafkaSettings | None = None, *, producer=None):
        self.settings = settings or KafkaSettings.from_env()
        self._lock = threading.Lock()
        self._closed = False
        config = self.settings.client_config()
        config.update({"enable.idempotence": True, "acks": "all",
                       "max.in.flight.requests.per.connection": 5,
                       "message.timeout.ms": self.settings.delivery_timeout_ms,
                       "request.timeout.ms": min(5000, self.settings.delivery_timeout_ms),
                       "allow.auto.create.topics": False,
                       "client.id": "finance-outbox"})
        self.producer = producer if producer is not None else _kafka().Producer(config)

    def publish(self, event: dict) -> None:
        account = event.get("account")
        if not isinstance(account, str) or not account.strip() or len(account) > 128:
            raise KafkaMessageError("Financial event requires a valid account key")
        try:
            payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError):
            raise KafkaMessageError("Financial event must contain JSON-compatible values") from None
        if len(payload) > MAX_EVENT_BYTES:
            raise KafkaMessageError("Financial event exceeds the message size limit")
        delivered = []

        def on_delivery(error, _message):
            delivered.append(error)

        with self._lock:
            if self._closed:
                raise KafkaTransportError("Kafka producer is closed")
            try:
                self.producer.produce(self.settings.topic, key=account.encode("utf-8"),
                                      value=payload, on_delivery=on_delivery)
                pending = self.producer.flush(self.settings.delivery_timeout_ms / 1000 + 1)
            except Exception as exc:
                # Do not persist raw library errors/configuration containing
                # broker credentials in the outbox's error field.
                raise KafkaTransportError(f"Kafka publish failed ({type(exc).__name__})") from None
            if not delivered:
                raise KafkaTransportError("Kafka delivery confirmation timed out; outcome is unknown")
            if delivered[0] is not None:
                raise KafkaTransportError("Kafka rejected delivery; event remains pending")
            if pending:
                raise KafkaTransportError("Kafka producer still has unconfirmed messages")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self.producer.flush(2)


class KafkaConsumerRunner:
    """One sequential consumer per projection and partition assignment."""

    def __init__(self, db, projection: str, settings: KafkaSettings | None = None, *,
                 consumer=None, service=None, group_id: str | None = None):
        if projection not in PROJECTIONS:
            raise ValueError("Unknown financial projection")
        self.settings = settings or KafkaSettings.from_env()
        self.projection = projection
        self._closed = False
        self._failed = False
        if service is None:
            from backend.projections import ProjectionService
            service = ProjectionService(db)
        self.service = service
        config = self.settings.client_config()
        config.update({"group.id": _name(group_id, "consumer group") if group_id else self.settings.group_id(projection),
                       "enable.auto.commit": False, "enable.auto.offset.store": False,
                       "auto.offset.reset": "earliest", "isolation.level": "read_committed",
                       "allow.auto.create.topics": False,
                       "max.poll.interval.ms": 300000,
                       "client.id": f"finance-{projection}"})
        self.consumer = consumer if consumer is not None else _kafka().Consumer(config)
        self.consumer.subscribe([self.settings.topic])

    def poll_once(self, timeout: float = 1.0) -> bool:
        if self._closed or self._failed:
            raise KafkaTransportError("Consumer stopped; restart before polling again")
        try:
            message = self.consumer.poll(timeout)
            if message is None:
                return False
            if message.error() is not None:
                raise KafkaTransportError("Kafka consumer received a broker error")
            raw = message.value()
            if not isinstance(raw, bytes) or len(raw) > MAX_EVENT_BYTES:
                raise KafkaMessageError("Kafka event is missing or exceeds the size limit")
            try:
                event = json.loads(raw.decode("utf-8"))
            except (UnicodeError, ValueError):
                raise KafkaMessageError("Kafka event is not valid UTF-8 JSON") from None
            if not isinstance(event, dict) or not isinstance(event.get("account"), str):
                raise KafkaMessageError("Kafka event must have an account")
            if message.key() != event["account"].encode("utf-8"):
                raise KafkaMessageError("Kafka partition key does not match the event account")
            self.service.process(self.projection, event)
            # process() returns only after committing its database transaction.
            # Even a deduplicated message must advance its Kafka offset.
            committed = self.consumer.commit(message=message, asynchronous=False)
            for partition in committed or ():
                error = getattr(partition, "error", None) or getattr(partition, "err", None)
                if error:
                    raise KafkaTransportError("Kafka offset commit failed")
            return True
        except BaseException:
            # Do not permit a caller to swallow a failed offset and keep polling
            # later records. Closing cannot auto-commit because it is disabled.
            self._failed = True
            raise

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self.consumer.close()


def init_topics(settings: KafkaSettings | None = None, *, partitions: int = 3,
                replication_factor: int = 1) -> None:
    settings = settings or KafkaSettings.from_env()
    if partitions < 1 or replication_factor < 1:
        raise ValueError("Kafka partitions and replication factor must be positive")
    kafka = _kafka()
    from confluent_kafka.admin import AdminClient, NewTopic
    admin = AdminClient(settings.client_config())
    future = admin.create_topics(
        [NewTopic(settings.topic, num_partitions=partitions, replication_factor=replication_factor)],
        request_timeout=settings.delivery_timeout_ms / 1000,
    )[settings.topic]
    try:
        future.result(timeout=settings.delivery_timeout_ms / 1000 + 1)
    except kafka.KafkaException as exc:
        if not exc.args or exc.args[0].code() != kafka.KafkaError.TOPIC_ALREADY_EXISTS:
            raise KafkaTransportError("Kafka topic initialization failed") from None
