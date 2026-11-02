"""Independent durable document worker: ``python -m backend.worker``."""

import argparse
import signal
import threading

from backend.db import Database
from backend.db import PROJECT_ROOT
from backend.jobs import JobService


def main(argv=None):
    import os
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
    parser = argparse.ArgumentParser(description="文档解析与OCR后台任务")
    parser.add_argument("--once", action="store_true", help="处理一个就绪任务后退出")
    parser.add_argument("--concurrency", type=int, choices=(1, 2), default=2)
    parser.add_argument("--poll-seconds", type=float, default=0.5)
    args = parser.parse_args(argv)
    db = Database()
    # Production schema changes are applied by alembic upgrade head before
    # starting API/worker. Creating absent tables keeps local startup simple.
    if os.environ.get("FINANCE_AUTO_CREATE_SCHEMA", "1").strip() != "0":
        db.create_schema()
    service = JobService(db, concurrency=args.concurrency, poll_seconds=args.poll_seconds)
    if args.once:
        try:
            service.run_once()
        finally:
            db.dispose()
        return 0
    stopped = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopped.set())
    service.start()
    try:
        while not stopped.wait(0.5):
            pass
    finally:
        service.stop()
        db.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
