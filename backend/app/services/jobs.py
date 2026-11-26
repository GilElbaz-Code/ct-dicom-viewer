"""Submitting work. Each DICOM directory becomes a row in `jobs` (what users see) plus a task in the RQ queue."""

import csv
import io
from functools import cache
from uuid import UUID, uuid4

from redis import Redis
from rq import Queue

from app.config import settings
from app.db import repository
from app.errors import InvalidInput
from app.services import ingest
from app.types import JobRecord

MAX_CSV_BYTES = 1_000_000


@cache
def queue() -> Queue:
    return Queue(connection=Redis.from_url(url=settings.redis_url, socket_connect_timeout=3, socket_timeout=3))


def publish(job_id: int) -> None:
    """Called by the relay. Duplicate queue messages are safe; PostgreSQL owns retries."""
    queue().enqueue(ingest.run, job_id, job_timeout=settings.job_timeout_seconds, result_ttl=0, failure_ttl=86400)


def submit(paths: list[str], batch_id: UUID | None = None) -> list[JobRecord]:
    return repository.create_jobs(paths=paths, batch_id=batch_id)


def submit_csv(content: bytes) -> list[JobRecord]:
    return submit(paths=parse_csv(content=content), batch_id=uuid4())


def parse_csv(content: bytes) -> list[str]:
    """One DICOM directory per row, in the first column. A 'path' header row and blank rows are skipped."""
    if len(content) > MAX_CSV_BYTES:
        raise InvalidInput("The CSV is larger than 1 MB")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise InvalidInput("The CSV must be UTF-8 text") from None
    try:
        paths = [row[0].strip() for row in csv.reader(io.StringIO(text), strict=True) if row and row[0].strip()]
    except csv.Error as exc:
        raise InvalidInput(f"Invalid CSV: {exc}") from exc
    if paths and paths[0].lower() == "path":
        paths = paths[1:]
    if not paths:
        raise InvalidInput("The CSV has no paths")
    return paths


def list_jobs(batch_id: UUID | None) -> list[JobRecord]:
    return repository.list_jobs(batch_id=batch_id)


def library_directories() -> list[str]:
    """Folders directly under DATA_DIR: the directories the UI offers to index."""
    root = settings.data_dir
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
