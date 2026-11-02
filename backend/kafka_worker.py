"""Kafka finance event processes: ``python -m backend.kafka_worker --help``."""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time

from backend.db import Database, PROJECT_ROOT
from backend.kafka_transport import (
    KafkaConsumerRunner, KafkaProducerTransport, KafkaSettings, init_topics,
)


def main(argv=None) -> int:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
    parser = argparse.ArgumentParser(description="账单 Kafka 事件发布与财务分析消费者")
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init-topics", help="创建财务事件主题（已存在则跳过）")
    initialize.add_argument("--partitions", type=int, default=3)
    initialize.add_argument("--replication-factor", type=int, default=1)
    publish = commands.add_parser("publish", help="发布数据库 Outbox 中的待投递事件")
    publish.add_argument("--once", action="store_true", help="尝试一个就绪事件后退出")
    publish.add_argument("--poll-seconds", type=float, default=0.5)
    consume = commands.add_parser("consume", help="运行独立统计或异常检测消费者组")
    consume.add_argument("--projection", choices=("summary", "anomalies"), required=True)
    consume.add_argument("--group-id", help="覆盖 Kafka 消费组；不会绕过数据库幂等记录")
    consume.add_argument("--once", action="store_true", help="成功处理一条消息后退出")
    consume.add_argument("--timeout-seconds", type=float, default=30,
                         help="--once 最长等待秒数；超时返回 2")
    commands.add_parser("backfill", help="为迁移前已有账单补齐缺失的 Outbox 事件")
    args = parser.parse_args(argv)
    if args.command == "publish" and not 0.05 <= args.poll_seconds <= 60:
        parser.error("--poll-seconds 必须在 0.05 到 60 之间")
    if args.command == "consume" and not 0 < args.timeout_seconds <= 3600:
        parser.error("--timeout-seconds 必须在 0 到 3600 之间")
    stopped = threading.Event()
    previous = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous[sig] = signal.signal(sig, lambda *_: stopped.set())
    db = client = None
    try:
        if args.command == "init-topics":
            init_topics(KafkaSettings.from_env(), partitions=args.partitions,
                        replication_factor=args.replication_factor)
            print("Kafka 财务主题已就绪")
            return 0
        db = Database()
        if os.environ.get("FINANCE_AUTO_CREATE_SCHEMA", "1").strip() != "0":
            db.create_schema()
        if args.command == "backfill":
            from backend.outbox import backfill_events
            print(f"已补齐 {backfill_events(db)} 条历史账单事件")
            return 0
        settings = KafkaSettings.from_env()
        if args.command == "publish":
            from backend.outbox import OutboxPublisher
            client = KafkaProducerTransport(settings)
            publisher = OutboxPublisher(db, client,
                                        lease_seconds=max(30, settings.delivery_timeout_ms / 1000 + 5))
            while not stopped.is_set():
                worked = publisher.publish_once()
                if args.once:
                    print("已尝试一个就绪事件" if worked else "暂无就绪事件")
                    return 0
                if not worked:
                    stopped.wait(args.poll_seconds)
        else:
            client = KafkaConsumerRunner(db, args.projection, settings, group_id=args.group_id)
            deadline = time.monotonic() + args.timeout_seconds if args.once else None
            while not stopped.is_set():
                timeout = min(1.0, max(0, deadline - time.monotonic())) if deadline else 1.0
                if client.poll_once(timeout=timeout) and args.once:
                    print("已处理一条财务事件")
                    return 0
                if deadline is not None and time.monotonic() >= deadline:
                    print("等待财务事件超时", file=sys.stderr)
                    return 2
        return 0
    except Exception as exc:
        # Configuration, DB URLs, Kafka library messages and events may contain
        # credentials or personal finance data. Keep console failures minimal.
        print(f"财务事件进程失败（{type(exc).__name__}）；检查配置及服务状态后重启，未确认消息不会跳过。", file=sys.stderr)
        return 1
    finally:
        cleanup_failed = False
        try:
            if client is not None:
                client.close()
        except Exception as exc:
            cleanup_failed = True
            print(f"关闭 Kafka 客户端失败（{type(exc).__name__}）", file=sys.stderr)
        finally:
            try:
                if db is not None:
                    db.dispose()
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
        if cleanup_failed:
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
