"""HTTP routes for submitting DICOM directories (one, or a CSV batch) and following the jobs."""

from uuid import UUID

from fastapi import APIRouter, UploadFile

from app.api.schemas import Job, SubmitPath
from app.services import jobs
from app.types import JobRecord

router = APIRouter(prefix="/api", tags=["jobs"])


@router.get("/directories", response_model=list[str])
def list_directories() -> list[str]:
    """Folders in the data directory, offered in the UI."""
    return jobs.library_directories()


@router.post("/jobs", status_code=201, response_model=Job)
def submit_directory(body: SubmitPath) -> JobRecord:
    """Index one DICOM directory (interactive)."""
    return jobs.submit(paths=[body.path])[0]


@router.post("/batches", status_code=201, response_model=list[Job])
def submit_batch(file: UploadFile) -> list[JobRecord]:
    """Index every directory listed in a CSV (batch). All its jobs share one batch_id."""
    return jobs.submit_csv(content=file.file.read(jobs.MAX_CSV_BYTES + 1))


@router.get("/jobs", response_model=list[Job])
def list_jobs(batch_id: UUID | None = None) -> list[JobRecord]:
    """The latest jobs, newest first; with batch_id, only that CSV's jobs."""
    return jobs.list_jobs(batch_id=batch_id)
