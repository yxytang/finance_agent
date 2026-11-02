"""Single-process API lifecycle; document workers may run independently."""
from pathlib import Path
import os

from backend.db import Database
from backend.jobs import JobService
from backend.ledger import LedgerRepository
from backend.session_store import SessionStore
from web.sessions import SessionManager


class Runtime:
    def __init__(self):
        self._closed = False
        self.manager = self.jobs = None
        self.db = Database()
        try:
            self._start()
        except BaseException:
            self.close()
            raise

    def _start(self):
        from fa.config import TRANSACTIONS_CSV
        from fa.categorize import categorize
        from fa.ingest import load_transactions

        # Local bootstrap only. Production startup requires `alembic upgrade head`.
        if os.environ.get("FINANCE_AUTO_CREATE_SCHEMA", "1") == "1":
            self.db.create_schema()
        self.ledger = LedgerRepository(self.db)
        source = Path(os.environ.get("FINANCE_BOOTSTRAP_CSV", str(TRANSACTIONS_CSV)))
        if source.is_file():
            tagged, _ = categorize(load_transactions(source), use_llm=False)
            self.ledger.import_csv(source, transactions=tagged)

        def factory(confirm, on_event):
            from fa.agent import Session
            from fa.tools import build_tools
            return Session(confirm=confirm, on_event=on_event, tools=build_tools(
                confirm, self.ledger.list_transactions,
                repository=self.ledger, database_url=self.db.url,
            ))

        self.manager = SessionManager(
            store=SessionStore(self.db), session_factory=factory,
            max_workers=int(os.environ.get("FINANCE_AGENT_WORKERS", "2")),
            max_pending=int(os.environ.get("FINANCE_AGENT_PENDING", "8")),
            recover_interrupted=True,
        )
        self.jobs = JobService(self.db)
        if os.environ.get("FINANCE_DOCUMENT_WORKER", "embedded") == "embedded":
            self.jobs.start()

    def close(self):
        from fa.tools import close_mcp
        if self._closed:
            return
        self._closed = True
        try:
            if self.manager:
                self.manager.shutdown(wait=True)
        finally:
            try:
                if self.jobs:
                    self.jobs.stop()
            finally:
                close_mcp(self.db.url)
                self.db.dispose()
