"""SQLAlchemy connection and transaction boundary shared by API and workers."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.models import Base


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def default_url() -> str:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
    return os.environ.get("FINANCE_DATABASE_URL", "").strip() or f"sqlite:///{(PROJECT_ROOT / 'data' / 'backend.sqlite3').as_posix()}"


class Database:
    def __init__(self, url: str | None = None):
        self.url = url or default_url()
        if self.url.startswith("postgres://"):
            self.url = "postgresql+psycopg://" + self.url[len("postgres://"):]
        elif self.url.startswith("postgresql://"):
            self.url = "postgresql+psycopg://" + self.url[len("postgresql://"):]
        parsed = make_url(self.url)
        options = {"pool_pre_ping": True}
        if parsed.get_backend_name() == "sqlite":
            options["connect_args"] = {"check_same_thread": False, "timeout": 30}
            if not parsed.database or parsed.database == ":memory:":
                options["poolclass"] = StaticPool
            else:
                Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(self.url, **options)
        if parsed.get_backend_name() == "sqlite":
            @event.listens_for(self.engine, "connect")
            def sqlite_settings(connection, _):
                cursor = connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=30000")
                if parsed.database and parsed.database != ":memory:":
                    cursor.execute("PRAGMA journal_mode=WAL")
                cursor.close()
        self._factory = sessionmaker(bind=self.engine, expire_on_commit=False)

    @contextmanager
    def session(self):
        """One request/task transaction, never a shared global ORM session."""
        with self._factory.begin() as session:
            yield session

    def create_schema(self) -> None:
        """Development bootstrap. Deployments should use Alembic migrations."""
        Base.metadata.create_all(self.engine)

    def dispose(self) -> None:
        self.engine.dispose()
