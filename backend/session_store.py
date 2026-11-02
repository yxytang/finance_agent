"""Durable conversation checkpoints and a cursor-addressed event journal.

Only completed turns checkpoint messages. Recovering an unfinished turn releases
its busy flag and records an interruption; it never replays tool side effects.
"""

from __future__ import annotations

import time

from sqlalchemy import delete, func, select, update

from backend.models import EventRecord, SessionRecord


class SessionStore:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def _snapshot(row):
        return {name: getattr(row, name) for name in (
            "id", "created_at", "last_activity", "turns", "busy", "messages", "next_seq"
        )}

    def create(self, session_id: str) -> dict:
        stamp = time.time()
        with self.db.session() as session:
            row = SessionRecord(id=session_id, created_at=stamp, last_activity=stamp,
                                turns=0, busy=False, messages=[], next_seq=0)
            session.add(row)
            session.flush()
            return self._snapshot(row)

    def get(self, session_id: str) -> dict | None:
        with self.db.session() as session:
            row = session.get(SessionRecord, session_id)
            return self._snapshot(row) if row else None

    def list(self) -> list[dict]:
        with self.db.session() as session:
            return [self._snapshot(row) for row in session.scalars(
                select(SessionRecord).order_by(SessionRecord.last_activity.desc())
            )]

    def count(self) -> int:
        with self.db.session() as session:
            return session.scalar(select(func.count()).select_from(SessionRecord))

    def delete(self, session_id: str) -> None:
        with self.db.session() as session:
            session.execute(delete(SessionRecord).where(SessionRecord.id == session_id))

    def claim(self, session_id: str) -> bool:
        with self.db.session() as session:
            result = session.execute(update(SessionRecord).where(
                SessionRecord.id == session_id, SessionRecord.busy.is_(False)
            ).values(busy=True, last_activity=time.time()))
            return result.rowcount == 1

    def release(self, session_id: str) -> None:
        with self.db.session() as session:
            session.execute(update(SessionRecord).where(SessionRecord.id == session_id)
                            .values(busy=False))

    def started(self, session_id: str) -> None:
        with self.db.session() as session:
            session.execute(update(SessionRecord).where(SessionRecord.id == session_id)
                            .values(turns=SessionRecord.turns + 1))

    @staticmethod
    def _append(session, session_id: str, event: dict) -> dict | None:
        # The UPDATE serializes concurrent writers on this session in both
        # PostgreSQL and SQLite. Sequence allocation and insertion commit together.
        seq = session.scalar(update(SessionRecord).where(SessionRecord.id == session_id)
                             .values(next_seq=SessionRecord.next_seq + 1,
                                     last_activity=time.time())
                             .returning(SessionRecord.next_seq))
        if seq is None:
            return None
        payload = {**event, "seq": seq}
        session.add(EventRecord(session_id=session_id, seq=seq, payload=payload))
        return payload

    def append(self, session_id: str, event: dict) -> dict | None:
        with self.db.session() as session:
            return self._append(session, session_id, event)

    def finish(self, session_id: str, messages: list | None) -> dict | None:
        with self.db.session() as session:
            values = {"busy": False}
            if messages is not None:
                values["messages"] = messages
            result = session.execute(update(SessionRecord).where(SessionRecord.id == session_id)
                                     .values(**values))
            if not result.rowcount:
                return None
            return self._append(session, session_id, {"type": "turn_end"})

    def events_after(self, session_id: str, after_seq: int = 0, *, limit: int = 500) -> list[dict]:
        with self.db.session() as session:
            return list(session.scalars(select(EventRecord.payload).where(
                EventRecord.session_id == session_id, EventRecord.seq > after_seq
            ).order_by(EventRecord.seq).limit(limit)))

    def recover_interrupted(self) -> int:
        """Call once before accepting requests in a single API-process deployment."""
        with self.db.session() as session:
            ids = list(session.scalars(select(SessionRecord.id).where(SessionRecord.busy.is_(True))))
            for session_id in ids:
                result = session.execute(update(SessionRecord).where(
                    SessionRecord.id == session_id, SessionRecord.busy.is_(True)
                ).values(busy=False))
                if not result.rowcount:
                    continue
                self._append(session, session_id, {
                    "type": "error", "message": "服务重启，上一轮已中断；已执行的操作不会自动撤销或重试。"
                })
                self._append(session, session_id, {"type": "confirm_resolved", "reason": "interrupted"})
                self._append(session, session_id, {"type": "turn_end", "interrupted": True})
            return len(ids)
