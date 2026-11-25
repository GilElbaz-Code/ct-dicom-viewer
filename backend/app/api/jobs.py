"""Submitting work: one directory (interactive), a CSV of directories (batch), or a browser upload.

All three paths create the same `ingest` job, so the logic after submission is shared.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, File, HTTPException, Query, UploadFile, status
from psycopg.errors import UniqueViolation
from pydantic import BaseModel, Field

from app import repository
from app.api.deps import AppSettings, Conn, User
from app.jobs import batches, queue
from app.sources import parse_batch_csv, resolve_source_path

router = APIRouter(tags=["jobs"])

JobStatus = Literal["queued", "running", "succeeded", "failed"]
JobKind = Literal["ingest", "analyze"]


class IngestRequest(BaseModel):
    path: str = Field(
        ..., description="DICOM directory, absolute or relative to the library root", examples=["sample_series_1"]
    )


# --- interactive ---------------------------------------------------------------------------


@router.post("/jobs", status_code=status.HTTP_201_CREATED)
def submit_ingest(body: IngestRequest, conn: Conn, settings: AppSettings, user: User) -> dict:
    """Index one DICOM directory and then analyze it. Path problems are reported immediately (400)."""
    directory = resolve_source_path(body.path, settings)
    job = queue.enqueue(
        conn, "ingest", submitted_by=user, max_attempts=settings.job_max_attempts, source_path=str(directory)
    )
    conn.commit()
    return job


@router.get("/jobs")
def list_jobs(
    conn: Conn,
    user: User,
    status_: Annotated[JobStatus | None, Query(alias="status")] = None,
    kind: JobKind | None = None,
    batch_id: int | None = None,
    series_id: int | None = None,
    mine: bool = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict]:
    return queue.list_jobs(
        conn,
        status=status_,
        kind=kind,
        batch_id=batch_id,
        series_id=series_id,
        submitted_by=user if mine else None,
        limit=limit,
        offset=offset,
    )


@router.get("/jobs/{job_id}")
def get_job(job_id: int, conn: Conn) -> dict:
    job = queue.get(conn, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: int, conn: Conn, settings: AppSettings) -> dict:
    """Requeue a failed job, e.g. after fixing the files on disk."""
    try:
        job = queue.retry_failed(conn, job_id, settings.job_max_attempts)
    except UniqueViolation:
        raise HTTPException(409, "This series already has an analysis job in progress") from None
    if job is None:
        raise HTTPException(409, "Only failed jobs can be retried")
    if job["kind"] == "analyze" and job["series_id"]:
        repository.set_series_status(conn, job["series_id"], "indexed")
    conn.commit()
    return job


# --- batch -----------------------------------------------------------------------------------


@router.post("/batches", status_code=status.HTTP_201_CREATED)
def submit_batch(
    conn: Conn,
    settings: AppSettings,
    user: User,
    file: Annotated[UploadFile, File(description="CSV with one DICOM directory per row")],
) -> dict:
    content = file.file.read(5 * 1024 * 1024 + 1)
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(413, "CSV is larger than 5 MB")
    try:
        rows = parse_batch_csv(content, settings.max_batch_rows)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    batch = batches.create_batch(conn, rows, filename=file.filename, user=user, settings=settings)
    conn.commit()
    return batch


@router.get("/batches")
def list_batches(conn: Conn, limit: Annotated[int, Query(ge=1, le=200)] = 50) -> list[dict]:
    return batches.list_batches(conn, limit=limit)


@router.get("/batches/{batch_id}")
def get_batch(batch_id: int, conn: Conn) -> dict:
    batch = batches.get_batch(conn, batch_id)
    if batch is None:
        raise HTTPException(404, "Batch not found")
    return batch


# --- upload from the browser -----------------------------------------------------------------


def _upload_dir(settings: AppSettings, upload_id: str) -> Path:
    try:
        uuid.UUID(upload_id)
    except ValueError:
        raise HTTPException(404, "Upload not found") from None
    path = settings.upload_root / upload_id
    if not path.is_dir():
        raise HTTPException(404, "Upload not found")
    return path


@router.post("/uploads", status_code=status.HTTP_201_CREATED)
def start_upload(settings: AppSettings) -> dict:
    """Open an upload session. The browser then sends the files in several smaller requests."""
    upload_id = str(uuid.uuid4())
    (settings.upload_root / upload_id).mkdir(parents=True)
    return {"upload_id": upload_id}


@router.post("/uploads/{upload_id}/files")
def upload_files(upload_id: str, settings: AppSettings, files: Annotated[list[UploadFile], File()]) -> dict:
    directory = _upload_dir(settings, upload_id)
    limit = settings.max_upload_file_mb * 1024 * 1024
    saved = 0
    for upload in files:
        name = Path(upload.filename or "").name  # drop any client-side folders: no path traversal
        if name in ("", ".", ".."):
            continue
        target = directory / name
        if target.exists():
            target = directory / f"{target.stem}_{uuid.uuid4().hex[:8]}{target.suffix}"
        written = 0
        with open(target, "wb") as out:
            while chunk := upload.file.read(1024 * 1024):
                written += len(chunk)
                if written > limit:
                    break
                out.write(chunk)
        if written > limit:
            os.remove(target)
            raise HTTPException(413, f"{name} is larger than {settings.max_upload_file_mb} MB")
        saved += 1
    return {"upload_id": upload_id, "saved": saved}


@router.post("/uploads/{upload_id}/complete", status_code=status.HTTP_201_CREATED)
def complete_upload(upload_id: str, conn: Conn, settings: AppSettings, user: User) -> dict:
    directory = _upload_dir(settings, upload_id)
    job = queue.enqueue(
        conn, "ingest", submitted_by=user, max_attempts=settings.job_max_attempts, source_path=str(directory)
    )
    conn.commit()
    return job


# --- server-side library -----------------------------------------------------------------------


@router.get("/library/directories")
def library_directories(
    conn: Conn, settings: AppSettings, max_depth: Annotated[int, Query(ge=1, le=6)] = 4
) -> list[dict]:
    """Directories under the library root that contain files: candidates for indexing."""
    root = settings.library_root
    if not root.is_dir():
        return []
    indexed = repository.indexed_source_paths(conn)
    found: list[dict] = []
    for current, dirs, files in os.walk(root):
        depth = len(Path(current).relative_to(root).parts)
        dirs[:] = sorted(d for d in dirs if not d.startswith(".")) if depth < max_depth else []
        visible = [f for f in files if not f.startswith(".")]
        if visible and current != str(root):
            resolved = str(Path(current).resolve())
            found.append(
                {
                    "path": str(Path(current).relative_to(root)),
                    "absolute_path": resolved,
                    "file_count": len(visible),
                    "series_id": indexed.get(resolved),
                }
            )
        if len(found) >= 500:
            break
    return found
