"""HTTP routes for submitting DICOM directories (one, or a CSV batch) and following the jobs."""

from http import HTTPStatus
from uuid import UUID

from fastapi import APIRouter, UploadFile

from api.schemas import INVALID, BeforeId, Job, Limit, SubmitPath
from core.types import JobRecord
from services import jobs

router = APIRouter(prefix="/api", tags=["jobs"])


@router.get("/directories", status_code=HTTPStatus.OK, response_model=list[str])
def list_directories() -> list[str]:
    """Folders in the data directory, offered in the UI."""
    return jobs.library_directories()


@router.post("/jobs", status_code=HTTPStatus.CREATED, response_model=Job, responses=INVALID)
def submit_directory(body: SubmitPath) -> JobRecord:
    """Index one DICOM directory (interactive)."""
    return jobs.submit(paths=[body.path])[0]


@router.post("/batches", status_code=HTTPStatus.CREATED, response_model=list[Job], responses=INVALID)
def submit_batch(file: UploadFile) -> list[JobRecord]:
    """Index every directory listed in a CSV (batch). All its jobs share one batch_id."""
    return jobs.submit_csv(content=file.file.read(jobs.MAX_CSV_BYTES + 1))


@router.get("/jobs", status_code=HTTPStatus.OK, response_model=list[Job], responses=INVALID)
def list_jobs(batch_id: UUID | None = None, before_id: BeforeId = None, limit: Limit = 100) -> list[JobRecord]:
    """Jobs, newest first, one page at a time; with batch_id, only that CSV's jobs."""
    return jobs.list_jobs(batch_id=batch_id, before_id=before_id, limit=limit)
