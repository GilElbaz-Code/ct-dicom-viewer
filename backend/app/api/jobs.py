"""HTTP routes for submitting DICOM directories (one, or a CSV batch) and following the jobs."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, UploadFile

from app.api.schemas import BeforeId, Job, Limit, SubmitPath
from app.services import jobs
from app.types import JobRecord

router = APIRouter(prefix="/api", tags=["jobs"])

# Optional. A client that retries a submission (after a timeout, say) sends the same key and gets the same jobs back.
IdempotencyKey = Annotated[
    str | None, Header(min_length=1, max_length=255, description="Repeat it to repeat a request safely")
]


@router.get("/directories", response_model=list[str])
def list_directories() -> list[str]:
    """Folders in the data directory, offered in the UI."""
    return jobs.library_directories()


@router.post("/jobs", status_code=201, response_model=Job)
def submit_directory(body: SubmitPath, idempotency_key: IdempotencyKey = None) -> JobRecord:
    """Index one DICOM directory (interactive)."""
    return jobs.submit(paths=[body.path], idempotency_key=idempotency_key)[0]


@router.post("/batches", status_code=201, response_model=list[Job])
def submit_batch(file: UploadFile, idempotency_key: IdempotencyKey = None) -> list[JobRecord]:
    """Index every directory listed in a CSV (batch). All its jobs share one batch_id."""
    return jobs.submit_csv(content=file.file.read(jobs.MAX_CSV_BYTES + 1), idempotency_key=idempotency_key)


@router.get("/jobs", response_model=list[Job])
def list_jobs(batch_id: UUID | None = None, before_id: BeforeId = None, limit: Limit = 100) -> list[JobRecord]:
    """Jobs, newest first, one page at a time; with batch_id, only that CSV's jobs."""
    return jobs.list_jobs(batch_id=batch_id, before_id=before_id, limit=limit)
