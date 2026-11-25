"""Job worker: claims jobs from PostgreSQL and runs them.

    python -m app.worker

Run several containers (`docker compose up --scale worker=3`) to process jobs
in parallel; SKIP LOCKED keeps them from colliding. SIGTERM lets the current
job finish before the process exits.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import traceback
import uuid
from pathlib import Path

from app.config import Settings, get_settings
from app.db import create_pool
from app.errors import JobError
from app.jobs import queue
from app.jobs.handlers import HANDLERS, JobContext
from app.logging_setup import configure_logging

log = logging.getLogger("worker")


class LeaseLostError(Exception):
    pass


class _LeaseKeeper(threading.Thread):
    """Extends the job's lease while the handler runs, so long jobs are not mistaken for dead ones."""

    def __init__(self, ctx: JobContext, job_id: int, worker_id: str) -> None:
        super().__init__(daemon=True, name=f"lease-{job_id}")
        self.ctx, self.job_id, self.worker_id = ctx, job_id, worker_id
        self.done = threading.Event()

    def run(self) -> None:
        # Short enough for the 60 s healthcheck: a long job must not look like a hung worker.
        interval = max(1.0, min(15.0, self.ctx.settings.job_lease_seconds / 3))
        while not self.done.wait(interval):
            Path(self.ctx.settings.worker_heartbeat_file).touch()
            try:
                with self.ctx.pool.connection() as conn:
                    if not queue.extend_lease(conn, self.job_id, self.worker_id, self.ctx.settings.job_lease_seconds):
                        log.warning("lost lease on job %s", self.job_id)
                        return
            except Exception:  # a DB blip should not kill the job; the next beat retries
                log.exception("lease extension failed for job %s", self.job_id)


def _as_job_error(exc: Exception) -> JobError:
    if isinstance(exc, JobError):
        return exc
    if isinstance(exc, MemoryError):
        return JobError("out_of_memory", "The worker ran out of memory processing this series", retryable=False)
    if isinstance(exc, OSError):
        return JobError("io_error", f"{type(exc).__name__}: {exc}", retryable=True)
    return JobError("internal_error", f"{type(exc).__name__}: {exc}", retryable=True)


class Worker:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.worker_id = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.pool = create_pool(settings.database_url, min_size=1, max_size=3, name="ctviewer-worker")
        self.ctx = JobContext(settings=settings, pool=self.pool)
        self.stopping = threading.Event()

    def request_stop(self, *_: object) -> None:
        log.info("stop requested; finishing the current job")
        self.stopping.set()

    def _beat(self) -> None:
        Path(self.settings.worker_heartbeat_file).touch()

    def run_forever(self) -> None:
        log.info("worker %s started (kinds: %s)", self.worker_id, ",".join(self.settings.worker_kinds))
        while not self.stopping.is_set():
            self._beat()
            try:
                processed = self.run_once()
            except Exception:  # database down etc.: log, back off, keep going
                log.exception("worker loop error")
                processed = False
            if not processed:
                self.stopping.wait(self.settings.worker_poll_seconds)
        self.pool.close()
        log.info("worker %s stopped", self.worker_id)

    def run_once(self) -> bool:
        """Claim and process one job. Returns False if the queue was empty."""
        with self.pool.connection() as conn:
            job = queue.claim(
                conn,
                worker_id=self.worker_id,
                kinds=self.settings.worker_kinds,
                lease_seconds=self.settings.job_lease_seconds,
            )
        if job is None:
            return False
        self.process(job)
        return True

    def process(self, job: dict) -> None:
        log.info("job %s (%s) attempt %s/%s", job["id"], job["kind"], job["attempts"], job["max_attempts"])
        handler = HANDLERS[job["kind"]]
        keeper = _LeaseKeeper(self.ctx, job["id"], self.worker_id)
        keeper.start()
        error: JobError | None = None
        try:
            if job["attempts"] > job["max_attempts"]:
                # Only reachable when earlier attempts died without reporting (crash, OOM kill).
                raise JobError("worker_lost", "The worker processing this job stopped responding too many times")
            payload = handler.run(job, self.ctx)
            with self.pool.connection() as conn:  # one transaction: results + job status
                result = handler.persist(conn, job, payload, self.ctx)
                if not queue.mark_succeeded(conn, job, self.worker_id, result):
                    raise LeaseLostError()
            log.info("job %s succeeded", job["id"])
            try:
                handler.after_commit(payload, self.ctx)
            except Exception:  # the job already succeeded; leftover files only cost disk space
                log.exception("job %s: cleanup after success failed", job["id"])
            return
        except LeaseLostError:
            log.warning("job %s: lease lost; another worker owns it now, result discarded", job["id"])
            return
        except Exception as exc:  # noqa: BLE001 - every failure must be recorded on the job
            error = _as_job_error(exc)
            if error.code in ("internal_error", "io_error"):
                log.error("job %s failed:\n%s", job["id"], traceback.format_exc())
        finally:
            keeper.done.set()

        with self.pool.connection() as conn:
            status = queue.record_failure(conn, job, self.worker_id, error)
            if status == "failed":
                handler.on_final_failure(conn, job, error)
        outcome = {"queued": "will retry", "failed": "failed"}.get(status, "lost its lease; not recorded")
        log.warning("job %s %s: [%s] %s", job["id"], outcome, error.code, error.message)


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    worker = Worker(settings)
    signal.signal(signal.SIGTERM, worker.request_stop)
    signal.signal(signal.SIGINT, worker.request_stop)
    worker.run_forever()


if __name__ == "__main__":
    main()
