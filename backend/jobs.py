"""Durable document ingestion queue shared by the HTTP API and workers.

Leases are renewed while parsing. Every state change from a worker is fenced by
its lease token, so a reclaimed job cannot be overwritten by an older worker.
Cancellation is cooperative: publishing an original file cannot be undone after
the import function has committed it.
"""

from __future__ import annotations

import errno
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import secrets
import tempfile
import threading
import time

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError

from backend.db import Database, PROJECT_ROOT
from backend.models import DocumentJob
from fa.knowledge import import_file, validate_filename
from fa.retrieval.documents import DocumentError, MAX_FILE_BYTES


TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


class _LeaseLost(Exception):
    pass


class _Cancelled(Exception):
    pass


class JobService:
    def __init__(self, db: Database, staging_dir: Path | None = None, *,
                 max_attempts: int = 3, lease_seconds: float = 30,
                 concurrency: int = 2, poll_seconds: float = 0.5):
        if max_attempts < 1 or lease_seconds <= 0 or not 1 <= concurrency <= 2:
            raise ValueError("Invalid worker limits")
        self.db = db
        configured_staging = os.environ.get("FINANCE_JOB_STAGING_DIR", "").strip()
        self.staging_dir = (staging_dir or (Path(configured_staging) if configured_staging else
                                            PROJECT_ROOT / "data" / "backend_jobs")).resolve()
        self.max_attempts = max_attempts
        self.lease_seconds = lease_seconds
        self.concurrency = concurrency
        self.poll_seconds = max(0.05, poll_seconds)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._lifecycle_lock = threading.Lock()

    @staticmethod
    def _public(job: DocumentJob) -> dict:
        return {"id": job.id, "name": job.filename, "status": job.status,
                "attempts": job.attempts, "max_attempts": job.max_attempts,
                "created_at": job.created_at, "updated_at": job.updated_at,
                "available_at": job.available_at, "error": job.error,
                "result": job.result, "cancel_requested": job.cancel_requested}

    def submit(self, root: Path, name: str, content: bytes) -> dict:
        validate_filename(name)
        if not content or len(content) > MAX_FILE_BYTES:
            raise DocumentError("不能上传空文件，单个文件不能超过20MB")
        root = root.resolve()
        digest = hashlib.sha256(content).hexdigest()
        key = hashlib.sha256((os.path.normcase(str(root)) + "\0" + name + "\0" + digest).encode()).hexdigest()
        with self.db.session() as session:
            existing = session.scalar(select(DocumentJob).where(DocumentJob.request_key == key))
            if existing:
                return self._public(existing)
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        job_id = secrets.token_hex(16)
        staged = self.staging_dir / (job_id + ".upload")
        # Finish writing before publishing the database record: workers cannot
        # observe partial input, including when API and worker are separate.
        with tempfile.NamedTemporaryFile(dir=self.staging_dir, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, staged)
        now = time.time()
        try:
            with self.db.session() as session:
                job = DocumentJob(id=job_id, root=str(root), filename=name,
                                  content_hash=digest, staged_path=str(staged),
                                  request_key=key, status="queued", attempts=0,
                                  max_attempts=self.max_attempts, available_at=now,
                                  created_at=now, updated_at=now, cancel_requested=False)
                session.add(job)
                session.flush()
                result = self._public(job)
        except IntegrityError:
            staged.unlink(missing_ok=True)
            with self.db.session() as session:
                winner = session.scalar(select(DocumentJob).where(DocumentJob.request_key == key))
                if winner is None:
                    raise
                return self._public(winner)
        except Exception:
            staged.unlink(missing_ok=True)
            raise
        return result

    def get(self, job_id: str) -> dict | None:
        with self.db.session() as session:
            job = session.get(DocumentJob, job_id)
            return self._public(job) if job else None

    def cancel(self, job_id: str) -> dict | None:
        now = time.time()
        with self.db.session() as session:
            # CAS avoids overwriting a concurrent worker's success or claim.
            session.execute(update(DocumentJob).where(
                DocumentJob.id == job_id, DocumentJob.status.in_(["queued", "retry_wait"])
            ).values(status="cancelled", cancel_requested=True, updated_at=now))
            session.execute(update(DocumentJob).where(
                DocumentJob.id == job_id, DocumentJob.status == "running"
            ).values(cancel_requested=True, updated_at=now))
        return self.get(job_id)

    def retry(self, job_id: str) -> dict | None:
        with self.db.session() as session:
            job = session.get(DocumentJob, job_id)
            if job is None:
                return None
            if job.status not in {"failed", "cancelled"}:
                raise ValueError("仅失败或已取消的任务可以重试")
            changed = session.execute(update(DocumentJob).where(
                DocumentJob.id == job_id, DocumentJob.status.in_(["failed", "cancelled"])
            ).values(status="queued", attempts=0, available_at=time.time(),
                     updated_at=time.time(), lease_token=None, lease_until=None,
                     cancel_requested=False, error=None, result=None))
            if not changed.rowcount:
                raise ValueError("任务状态已变化")
        return self.get(job_id)

    def _claim(self) -> dict | None:
        now = time.time()
        eligible = or_(and_(DocumentJob.status.in_(["queued", "retry_wait"]),
                           DocumentJob.available_at <= now),
                       and_(DocumentJob.status == "running", DocumentJob.lease_until <= now))
        with self.db.session() as session:
            # Exhausted crashed attempts must become terminal rather than stay
            # running forever. Cancellation takes priority for abandoned jobs.
            session.execute(update(DocumentJob).where(
                eligible, DocumentJob.cancel_requested.is_(True)
            ).values(status="cancelled", lease_token=None, lease_until=None, updated_at=now))
            session.execute(update(DocumentJob).where(
                eligible, DocumentJob.attempts >= DocumentJob.max_attempts
            ).values(status="failed", error="处理进程中断，已达到最大尝试次数",
                     lease_token=None, lease_until=None, updated_at=now))
            candidates = session.scalars(select(DocumentJob.id).where(
                eligible, DocumentJob.attempts < DocumentJob.max_attempts,
                DocumentJob.cancel_requested.is_(False)
            ).order_by(DocumentJob.created_at, DocumentJob.id).limit(20)).all()
            for job_id in candidates:
                token = secrets.token_hex(16)
                claimed = session.execute(update(DocumentJob).where(
                    DocumentJob.id == job_id, eligible,
                    DocumentJob.attempts < DocumentJob.max_attempts,
                    DocumentJob.cancel_requested.is_(False)
                ).values(status="running", attempts=DocumentJob.attempts + 1,
                         lease_token=token, lease_until=now + self.lease_seconds,
                         updated_at=now, error=None))
                if claimed.rowcount:
                    job = session.get(DocumentJob, job_id)
                    return {"id": job.id, "token": token, "root": job.root,
                            "name": job.filename, "path": job.staged_path,
                            "hash": job.content_hash, "attempts": job.attempts,
                            "max_attempts": job.max_attempts}
        return None

    def _renew(self, claim: dict) -> bool:
        now = time.time()
        with self.db.session() as session:
            changed = session.execute(update(DocumentJob).where(
                DocumentJob.id == claim["id"], DocumentJob.status == "running",
                DocumentJob.lease_token == claim["token"], DocumentJob.lease_until > now
            ).values(lease_until=now + self.lease_seconds, updated_at=now))
            return bool(changed.rowcount)

    def _finish(self, claim: dict, status: str, *, result=None, error=None,
                delay: float = 0) -> bool:
        now = time.time()
        with self.db.session() as session:
            changed = session.execute(update(DocumentJob).where(
                DocumentJob.id == claim["id"], DocumentJob.status == "running",
                DocumentJob.lease_token == claim["token"], DocumentJob.lease_until > now
            ).values(status=status, result=result, error=error,
                     updated_at=now, available_at=now + delay,
                     lease_token=None, lease_until=None))
            return bool(changed.rowcount)

    def _published(self, claim: dict, content: bytes) -> dict | None:
        root = Path(claim["root"]).resolve()
        path = root / "uploads" / claim["name"]
        if not path.resolve().is_relative_to(root):
            raise DocumentError("上传目录不在知识库内")
        if not path.is_file():
            return None
        expected = content
        if path.suffix.lower() == ".md":
            try:
                # import_file writes Markdown as UTF-8 text with platform
                # newline conversion (and strips a possible UTF-8 BOM).
                expected = content.decode("utf-8-sig").replace("\n", os.linesep).encode("utf-8")
            except UnicodeDecodeError:
                raise DocumentError("Markdown需要UTF-8编码")
        if hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(expected).digest():
            raise FileExistsError("同名文件已存在且内容不同，请重命名后上传")
        from fa.retrieval.index import load_or_build
        index = load_or_build(root, root / ".index" / "index.json")
        source = path.relative_to(root).as_posix()
        chunks = [chunk for chunk in index.chunks if chunk.source == source]
        if not chunks:
            raise DocumentError("已发布的原件没有可检索内容")
        return {"source": source, "name": claim["name"], "size": len(content),
                "chunks": len(chunks), "recovered": True,
                "locations": list(dict.fromkeys(chunk.breadcrumb for chunk in chunks)),
                "methods": sorted({chunk.extraction_method or "markdown" for chunk in chunks})}

    @contextmanager
    def _ingestion_lock(self, root: Path, claim: dict, lost: threading.Event):
        """Serialize backend publishing/index rebuilds across worker processes.

        A process-local RLock cannot protect the filesystem index from a second
        worker process. OS advisory locks are released automatically on process
        exit; leases are renewed independently while this lock is awaited.
        """
        folder = root.resolve() / ".index"
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / "document-ingestion.lock").open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            acquired = False
            try:
                while not acquired:
                    if lost.is_set():
                        raise _LeaseLost()
                    state = self.get(claim["id"])
                    if state is None or state["status"] != "running":
                        raise _LeaseLost()
                    if state["cancel_requested"]:
                        raise _Cancelled()
                    try:
                        if os.name == "nt":
                            import msvcrt
                            handle.seek(0)
                            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        acquired = True
                    except OSError as exc:
                        if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                            raise
                        lost.wait(0.05)
                yield
            finally:
                if acquired:
                    if os.name == "nt":
                        import msvcrt
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _temporary(exc: Exception) -> bool:
        if isinstance(exc, OSError) and getattr(exc, "winerror", None) in {32, 33}:
            return True
        if isinstance(exc, (DocumentError, FileExistsError, FileNotFoundError, PermissionError)):
            return False
        return isinstance(exc, (TimeoutError, ConnectionError)) or (
            isinstance(exc, OSError) and exc.errno in {
                errno.EAGAIN, errno.EBUSY, errno.EINTR, errno.EIO, errno.ENOSPC,
                errno.ETIMEDOUT, errno.ECONNRESET, errno.ECONNREFUSED,
            })

    def run_once(self) -> bool:
        claim = self._claim()
        if claim is None:
            return False
        done = threading.Event()
        lost = threading.Event()

        def heartbeat():
            while not done.wait(max(0.05, self.lease_seconds / 3)):
                try:
                    if not self._renew(claim):
                        lost.set()
                        return
                except Exception:
                    # Do not keep publishing under an unverified lease.
                    lost.set()
                    return

        thread = threading.Thread(target=heartbeat, name="document-lease", daemon=True)
        thread.start()
        try:
            content = Path(claim["path"]).read_bytes()
            if hashlib.sha256(content).hexdigest() != claim["hash"]:
                raise DocumentError("暂存文件校验失败")
            current = self.get(claim["id"])
            if current["cancel_requested"]:
                self._finish(claim, "cancelled")
                return True
            if lost.is_set():
                return True
            with self._ingestion_lock(Path(claim["root"]), claim, lost):
                # The lease may have expired while waiting for another process
                # to finish OCR. Revalidate it immediately before filesystem
                # mutation, not just on the heartbeat's next interval.
                if lost.is_set() or not self._renew(claim):
                    raise _LeaseLost()
                result = self._published(claim, content)
                if result is None:
                    try:
                        result = import_file(Path(claim["root"]), claim["name"], content)
                    except FileExistsError:
                        result = self._published(claim, content)
                        if result is None:
                            raise
            # Import may already be committed when cancellation arrives. Report
            # success truthfully instead of promising a rollback we cannot do.
            if not lost.is_set():
                if self._finish(claim, "succeeded", result=result):
                    # The committed original is now the source of truth, and
                    # successful jobs cannot be retried. Keep failed/cancelled
                    # inputs for explicit retry, but release successful inputs.
                    staged = Path(claim["path"])
                    if staged.resolve().is_relative_to(self.staging_dir):
                        try:
                            staged.unlink(missing_ok=True)
                        except OSError:
                            # Cleanup must not turn a committed import into a
                            # failure. An operator can remove orphaned uploads.
                            pass
        except _LeaseLost:
            pass
        except _Cancelled:
            self._finish(claim, "cancelled")
        except Exception as exc:
            current = self.get(claim["id"])
            if not lost.is_set():
                if current and current["cancel_requested"]:
                    self._finish(claim, "cancelled")
                else:
                    retry = self._temporary(exc) and claim["attempts"] < claim["max_attempts"]
                    self._finish(claim, "retry_wait" if retry else "failed",
                                 error=f"{type(exc).__name__}: {str(exc)[:500]}",
                                 delay=min(60, 2 ** claim["attempts"]) if retry else 0)
        finally:
            done.set()
            thread.join(timeout=1)
        return True

    def start(self) -> None:
        with self._lifecycle_lock:
            if any(thread.is_alive() for thread in self._threads):
                return
            self._stop.clear()

            def loop():
                while not self._stop.is_set():
                    try:
                        worked = self.run_once()
                    except Exception:
                        worked = False
                    if not worked:
                        self._stop.wait(self.poll_seconds)

            self._threads = [threading.Thread(target=loop, name=f"document-worker-{i}", daemon=True)
                             for i in range(self.concurrency)]
            for thread in self._threads:
                thread.start()

    def stop(self, timeout: float | None = None) -> bool:
        """Stop claiming and drain current parsers before disposing the DB.

        Native OCR cannot be safely killed in a Python thread. By default this
        waits for it to return. An explicit timeout returns False while threads
        remain alive; callers must keep the database usable in that case.
        """
        self._stop.set()
        deadline = time.monotonic() + timeout if timeout is not None else None
        for thread in self._threads:
            thread.join(timeout=max(0, deadline - time.monotonic()) if deadline is not None else None)
        return not any(thread.is_alive() for thread in self._threads)
