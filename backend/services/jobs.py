"""Submitting work. Each DICOM directory becomes a row in `jobs` (what users see) plus a task in the RQ queue."""

import csv
import io
from functools import cache
from uuid import UUID, uuid4

from redis import Redis
from rq import Queue
from rq.job import Job, JobStatus

from core.config import settings
from core.errors import InvalidInput
from core.types import JobRecord
from db import repository
from services import ingest

MAX_CSV_BYTES = 1_000_000
# Data rows per CSV (one job each). Bounds one request's transaction, response and queue additions; it does not
# limit how many jobs several requests can queue in total.
MAX_CSV_ROWS = 1_000


REDIS_TIMEOUT_SECONDS = 3  # a hung Redis fails the relay's pass quickly; the outbox retries it


@cache
def queue() -> Queue:
    return Queue(
        connection=Redis.from_url(
            url=settings.redis_url, socket_connect_timeout=REDIS_TIMEOUT_SECONDS, socket_timeout=REDIS_TIMEOUT_SECONDS
        )
    )


def publish(job_ids: list[int]) -> None:
    """Called by the relay, two Redis round trips per batch. A job still waiting in the queue is not added twice
    (a second copy would only inflate the queue length); any other duplicate is harmless."""
    q = queue()
    rq_ids = [f"ingest-{job_id}" for job_id in job_ids]
    waiting = {
        job.id
        for job in Job.fetch_many(rq_ids, connection=q.connection)
        if job is not None and job.get_status(refresh=False) == JobStatus.QUEUED  # fetch_many already loaded it
    }
    batch = [
        Queue.prepare_data(
            ingest.run,
            (job_id,),
            job_id=rq_id,
            # A backstop only: Lease.check stops the attempt first, with a reason the user can read.
            timeout=settings.job_timeout_seconds + 60,
            result_ttl=0,
            failure_ttl=86400,
        )
        for job_id, rq_id in zip(job_ids, rq_ids, strict=True)
        if rq_id not in waiting
    ]
    if batch:
        q.enqueue_many(batch)


def submit(paths: list[str], batch_id: UUID | None = None) -> list[JobRecord]:
    return repository.create_jobs(paths=paths, batch_id=batch_id)


def submit_csv(content: bytes) -> list[JobRecord]:
    return submit(paths=parse_csv(content=content), batch_id=uuid4())


def parse_csv(content: bytes) -> list[str]:
    """One DICOM directory per row, in the first column. A 'path' header row and blank rows are skipped.
    More than MAX_CSV_ROWS data rows reject the whole CSV, found while reading: the rest is never collected."""
    if len(content) > MAX_CSV_BYTES:
        raise InvalidInput("The CSV is larger than 1 MB")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise InvalidInput("The CSV must be UTF-8 text") from None
    paths: list[str] = []
    header_checked = False
    try:
        for row in csv.reader(io.StringIO(text), strict=True):
            path = row[0].strip() if row else ""
            if not path:
                continue
            if not header_checked:  # only the first nonblank row can be the header
                header_checked = True
                if path.lower() == "path":
                    continue
            if len(paths) == MAX_CSV_ROWS:
                raise InvalidInput(f"The CSV lists more than {MAX_CSV_ROWS:,} folders; split it into several files")
            paths.append(path)
    except csv.Error as exc:
        raise InvalidInput(f"Invalid CSV: {exc}") from exc
    if not paths:
        raise InvalidInput("The CSV has no paths")
    return paths


def list_jobs(batch_id: UUID | None, before_id: int | None, limit: int) -> list[JobRecord]:
    return repository.list_jobs(batch_id=batch_id, before_id=before_id, limit=limit)


def library_directories() -> list[str]:
    """Folders directly under DATA_DIR: the directories the UI offers to index."""
    root = settings.data_dir
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
