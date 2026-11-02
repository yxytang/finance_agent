"""Queue tests use real database transactions and real Markdown ingestion."""

import errno
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

import pytest
from sqlalchemy import create_engine, text, update
from sqlalchemy.engine import make_url

from backend.db import Database
from backend.jobs import JobService
from backend.models import DocumentJob
from fa.knowledge import import_file
from fa.retrieval.documents import DocumentError


CONTENT = ("# Emergency reserve\n\nKeep six months of basic living costs in an accessible "
           "reserve account. Reconcile balances with the original statements.\n").encode()


@pytest.fixture(params=["sqlite", "postgresql"])
def service(tmp_path, request):
    admin = None
    schema = None
    if request.param == "postgresql":
        url = os.environ.get("FINANCE_TEST_DATABASE_URL", "")
        if not url:
            pytest.skip("FINANCE_TEST_DATABASE_URL is needed for PostgreSQL integration")
        admin = create_engine(url)
        schema = "jobs_test_" + uuid.uuid4().hex
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        isolated = make_url(url).update_query_dict({"options": f"-csearch_path={schema}"})
        db = Database(isolated.render_as_string(hide_password=False))
    else:
        db = Database(f"sqlite:///{(tmp_path / 'jobs.sqlite3').as_posix()}")
    db.create_schema()
    service = JobService(db, tmp_path / "staging", lease_seconds=2)
    yield service
    service.stop()
    db.dispose()
    if admin is not None:
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def ready(service, job_id):
    with service.db.session() as session:
        session.execute(update(DocumentJob).where(DocumentJob.id == job_id)
                        .values(available_at=time.time() - 1))


def test_submit_is_idempotent_and_rebuilt_worker_restores_queue(service, tmp_path):
    root = tmp_path / "knowledge"
    first = service.submit(root, "reserve.md", CONTENT)
    assert first["status"] == "queued"
    assert service.submit(root, "reserve.md", CONTENT)["id"] == first["id"]
    restored = JobService(service.db, service.staging_dir)
    assert restored.run_once()
    result = restored.get(first["id"])
    assert result["status"] == "succeeded"
    assert result["result"]["source"] == "uploads/reserve.md"
    assert result["result"]["chunks"] > 0
    assert not restored.run_once()
    assert (root / "uploads" / "reserve.md").read_text() == CONTENT.decode()
    assert not list(service.staging_dir.glob("*.upload"))
    assert service.submit(root, "reserve.md", CONTENT)["id"] == first["id"]


def test_two_workers_cannot_claim_same_job(service, tmp_path):
    job = service.submit(tmp_path / "knowledge", "reserve.md", CONTENT)
    other = JobService(service.db, service.staging_dir)
    barrier = threading.Barrier(2)
    claims = []

    def claim(worker):
        barrier.wait()
        claims.append(worker._claim())

    workers = [threading.Thread(target=claim, args=(worker,)) for worker in (service, other)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(5)
        assert not worker.is_alive()
    assert len([claim for claim in claims if claim]) == 1
    assert service.get(job["id"])["attempts"] == 1


def test_expired_lease_reclaimed_and_old_worker_fenced(service, tmp_path):
    job = service.submit(tmp_path / "knowledge", "reserve.md", CONTENT)
    old = service._claim()
    with service.db.session() as session:
        session.execute(update(DocumentJob).where(DocumentJob.id == job["id"])
                        .values(lease_until=time.time() - 1))
    new = service._claim()
    assert new and new["token"] != old["token"]
    assert new["attempts"] == 2
    assert not service._renew(old)
    assert not service._finish(old, "succeeded", result={"wrong": True})
    assert service._finish(new, "succeeded", result={"right": True})
    assert service.get(job["id"])["result"] == {"right": True}


def test_crash_after_original_published_is_recovered(service, tmp_path):
    root = tmp_path / "knowledge"
    job = service.submit(root, "reserve.md", CONTENT)
    claim = service._claim()
    import_file(root, "reserve.md", CONTENT)
    with service.db.session() as session:
        session.execute(update(DocumentJob).where(DocumentJob.id == job["id"])
                        .values(lease_until=time.time() - 1))
    assert service.run_once()
    result = service.get(job["id"])
    assert result["status"] == "succeeded"
    assert result["result"]["recovered"]
    assert not service._finish(claim, "failed", error="stale")


def test_recovery_does_not_overwrite_different_original(service, tmp_path):
    root = tmp_path / "knowledge"
    import_file(root, "reserve.md", CONTENT)
    different = CONTENT + b"Another rule applies."
    job = service.submit(root, "reserve.md", different)
    service.run_once()
    assert service.get(job["id"])["status"] == "failed"
    assert (root / "uploads" / "reserve.md").read_text() == CONTENT.decode()


def test_transient_io_retry_then_success(service, tmp_path, monkeypatch):
    from backend import jobs
    original = jobs.import_file
    calls = []

    def flaky(*args):
        calls.append(1)
        if len(calls) == 1:
            raise OSError(errno.EBUSY, "temporary busy")
        return original(*args)

    monkeypatch.setattr(jobs, "import_file", flaky)
    job = service.submit(tmp_path / "knowledge", "reserve.md", CONTENT)
    service.run_once()
    assert service.get(job["id"])["status"] == "retry_wait"
    assert not service.run_once()
    ready(service, job["id"])
    service.run_once()
    assert service.get(job["id"])["status"] == "succeeded"
    assert service.get(job["id"])["attempts"] == 2


def test_deterministic_error_is_not_retried_and_manual_retry_works(service, tmp_path, monkeypatch):
    from backend import jobs
    original = jobs.import_file

    def invalid(*_):
        raise DocumentError("invalid document")

    monkeypatch.setattr(jobs, "import_file", invalid)
    job = service.submit(tmp_path / "knowledge", "reserve.md", CONTENT)
    service.run_once()
    assert service.get(job["id"])["status"] == "failed"
    assert service.get(job["id"])["attempts"] == 1
    monkeypatch.setattr(jobs, "import_file", original)
    assert service.retry(job["id"])["status"] == "queued"
    service.run_once()
    assert service.get(job["id"])["status"] == "succeeded"
    with pytest.raises(ValueError):
        service.retry(job["id"])


def test_retry_limit_applies_to_io_and_abandoned_claims(service, tmp_path, monkeypatch):
    from backend import jobs

    def unavailable(*_):
        raise TimeoutError("temporary unavailable")

    monkeypatch.setattr(jobs, "import_file", unavailable)
    job = service.submit(tmp_path / "knowledge", "reserve.md", CONTENT)
    for _ in range(3):
        ready(service, job["id"])
        assert service.run_once()
    assert service.get(job["id"])["status"] == "failed"
    assert not service.run_once()
    second = service.submit(tmp_path / "knowledge", "other.md", CONTENT)
    with service.db.session() as session:
        session.execute(update(DocumentJob).where(DocumentJob.id == second["id"])
                        .values(status="running", attempts=3, lease_until=time.time() - 1))
    assert not service.run_once()
    assert service.get(second["id"])["status"] == "failed"


def test_cancel_queued_and_retry(service, tmp_path):
    job = service.submit(tmp_path / "knowledge", "reserve.md", CONTENT)
    assert service.cancel(job["id"])["status"] == "cancelled"
    assert not service.run_once()
    assert service.retry(job["id"])["status"] == "queued"
    service.run_once()
    assert service.get(job["id"])["status"] == "succeeded"
    assert service.cancel("missing") is None
    assert service.retry("missing") is None


def test_running_cancel_reports_committed_import_truthfully(service, tmp_path, monkeypatch):
    from backend import jobs
    original = jobs.import_file
    entered, resume = threading.Event(), threading.Event()

    def blocked(*args):
        entered.set()
        assert resume.wait(5)
        return original(*args)

    monkeypatch.setattr(jobs, "import_file", blocked)
    job = service.submit(tmp_path / "knowledge", "reserve.md", CONTENT)
    worker = threading.Thread(target=service.run_once)
    worker.start()
    assert entered.wait(5)
    assert service.cancel(job["id"])["cancel_requested"]
    resume.set()
    worker.join(5)
    assert service.get(job["id"])["status"] == "succeeded"
    assert service.get(job["id"])["cancel_requested"]


def test_heartbeat_renews_long_import(service, tmp_path, monkeypatch):
    from backend import jobs
    original = jobs.import_file
    entered, resume = threading.Event(), threading.Event()
    service.lease_seconds = 0.3

    def blocked(*args):
        entered.set()
        assert resume.wait(5)
        return original(*args)

    monkeypatch.setattr(jobs, "import_file", blocked)
    job = service.submit(tmp_path / "knowledge", "reserve.md", CONTENT)
    worker = threading.Thread(target=service.run_once)
    worker.start()
    assert entered.wait(5)
    time.sleep(0.7)
    assert JobService(service.db, service.staging_dir)._claim() is None
    resume.set()
    worker.join(5)
    assert service.get(job["id"])["status"] == "succeeded"


def test_upload_validation_and_staging_checksum(service, tmp_path):
    for name, content in [("../bad.pdf", CONTENT), ("empty.pdf", b"")]:
        with pytest.raises(DocumentError):
            service.submit(tmp_path / "knowledge", name, content)
    job = service.submit(tmp_path / "knowledge", "reserve.md", CONTENT)
    with service.db.session() as session:
        staged = session.get(DocumentJob, job["id"]).staged_path
    Path(staged).write_bytes(b"tampered")
    service.run_once()
    assert service.get(job["id"])["status"] == "failed"
    assert "校验" in service.get(job["id"])["error"]


def test_concurrent_submit_returns_same_durable_job(service, tmp_path):
    barrier = threading.Barrier(2)
    results = []

    def submit():
        barrier.wait()
        results.append(service.submit(tmp_path / "knowledge", "reserve.md", CONTENT))

    threads = [threading.Thread(target=submit) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert len(results) == 2
    assert results[0]["id"] == results[1]["id"]
    assert len(list(service.staging_dir.glob("*.upload"))) == 1


def test_stop_waits_for_current_parser_before_database_disposal(service, tmp_path, monkeypatch):
    from backend import jobs
    original = jobs.import_file
    entered, resume = threading.Event(), threading.Event()

    def blocked(*args):
        entered.set()
        assert resume.wait(10)
        return original(*args)

    monkeypatch.setattr(jobs, "import_file", blocked)
    job = service.submit(tmp_path / "knowledge", "reserve.md", CONTENT)
    service.start()
    assert entered.wait(5)
    assert not service.stop(timeout=0.05)
    assert service.get(job["id"])["status"] == "running"
    resume.set()
    assert service.stop()
    assert service.get(job["id"])["status"] == "succeeded"


def test_staging_directory_can_be_shared_through_environment(service, tmp_path, monkeypatch):
    shared = tmp_path / "shared-staging"
    monkeypatch.setenv("FINANCE_JOB_STAGING_DIR", str(shared))
    worker = JobService(service.db)
    assert worker.staging_dir == shared.resolve()
    job = worker.submit(tmp_path / "knowledge", "reserve.md", CONTENT)
    with service.db.session() as session:
        assert Path(session.get(DocumentJob, job["id"]).staged_path).parent == shared.resolve()


def test_separate_worker_processes_serialize_import_and_keep_both_index_sources(service, tmp_path):
    root = tmp_path / "knowledge"
    first = service.submit(root, "first.md", CONTENT)
    second = service.submit(root, "second.md", CONTENT + b" Extra reserve guidelines.")
    script = tmp_path / "worker_probe.py"
    script.write_text('''import sys, time
from pathlib import Path
from backend.db import Database
from backend import jobs
db = Database(sys.argv[1])
service = jobs.JobService(db, Path(sys.argv[2]), lease_seconds=2)
entered, release = Path(sys.argv[3]), Path(sys.argv[4])
original = jobs.import_file
def blocked(*args):
    entered.write_text("entered")
    deadline = time.monotonic() + 15
    while not release.exists():
        if time.monotonic() > deadline:
            raise TimeoutError("test lock release timeout")
        time.sleep(.03)
    return original(*args)
jobs.import_file = blocked
service.run_once()
db.dispose()
''', encoding="utf-8")
    entered_a, entered_b = tmp_path / "a-entered", tmp_path / "b-entered"
    release_a, release_b = tmp_path / "a-release", tmp_path / "b-release"
    release_b.write_text("ready")

    def launch(marker, release):
        # -c imports the test helper with the repository on sys.path, even when
        # pytest invokes Python through a platform-specific virtualenv shim.
        return subprocess.Popen([sys.executable, "-c",
                                 "import runpy,sys; runpy.run_path(sys.argv.pop(1),run_name='__main__')",
                                 str(script), service.db.url, str(service.staging_dir), str(marker), str(release)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def wait_for(predicate):
        deadline = time.monotonic() + 12
        while not predicate():
            if time.monotonic() > deadline:
                raise AssertionError("worker did not reach expected state")
            time.sleep(0.03)

    processes = []
    try:
        processes.append(launch(entered_a, release_a))
        wait_for(entered_a.exists)
        processes.append(launch(entered_b, release_b))
        wait_for(lambda: service.get(second["id"])["status"] == "running")
        time.sleep(0.2)
        assert not entered_b.exists(), "second process entered index mutation before lock release"
        release_a.write_text("ready")
        for process in processes:
            stdout, stderr = process.communicate(timeout=12)
            assert process.returncode == 0, (stdout + stderr).decode(errors="replace")
        assert service.get(first["id"])["status"] == "succeeded"
        assert service.get(second["id"])["status"] == "succeeded"
        from fa.retrieval.index import load
        index = load(root / ".index" / "index.json")
        assert {chunk.source for chunk in index.chunks} == {"uploads/first.md", "uploads/second.md"}
    finally:
        release_a.write_text("ready")
        for process in processes:
            if process.poll() is None:
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()


def test_worker_can_require_preapplied_schema(monkeypatch):
    from backend import worker
    calls = []

    class FakeDatabase:
        def create_schema(self):
            raise AssertionError("automatic schema creation should be disabled")

        def dispose(self):
            calls.append("disposed")

    class FakeService:
        def __init__(self, *args, **kwargs):
            pass

        def run_once(self):
            calls.append("processed")

    monkeypatch.setenv("FINANCE_AUTO_CREATE_SCHEMA", "0")
    monkeypatch.setattr(worker, "Database", FakeDatabase)
    monkeypatch.setattr(worker, "JobService", FakeService)
    assert worker.main(["--once"]) == 0
    assert calls == ["processed", "disposed"]
