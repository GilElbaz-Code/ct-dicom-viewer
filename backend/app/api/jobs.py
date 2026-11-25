"""Submitting work: one directory (interactive), a CSV of directories (batch), or a browser upload.

All three paths create the same `ingest` job, so the logic after submission is shared.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, Query, UploadFile, status
from psycopg.errors import UniqueViolation
from pydantic import BaseModel, Field

from app import repository
from app.api.deps import AppSettings, Conn, User
from app.api.schemas import (
    BatchCreated,
    BatchSummary,
    Job,
    JobKind,
    JobStatus,
    LibraryDirectory,
    UploadResult,
    UploadSession,
)
from app.jobs import batches, queue
from app.sources import parse_batch_csv, resolve_source_path

router = APIRouter(tags=["jobs"])


class IngestRequest(BaseModel):
    path: str = Field(
        ..., description="DICOM directory, absolute or relative to the library root", examples=["sample_series_1"]
    )


# --- interactive ---------------------------------------------------------------------------


@router.post("/jobs", status_code=status.HTTP_201_CREATED, response_model=Job)
def submit_ingest(body: IngestRequest, conn: Conn, settings: AppSettings, user: User) -> dict:
    """Index one DICOM directory and then analyze it. Path problems are reported immediately (400)."""
    directory = resolve_source_path(body.path, settings)
    job = queue.enqueue(
        conn, "ingest", submitted_by=user, max_attempts=settings.job_max_attempts, source_path=str(directory)
    )
    conn.commit()
    return job


@router.get("/jobs", response_model=list[Job])
def list_jobs(
    conn: Conn,
    user: User,
    status_: Annotated[JobStatus | None, Query(alias="status")] = None,
    kind: JobKind | None = None,
    batch_id: int | None = None,
    series_id: int | None = None,
    mine: bool = False,
    before_id: Annotated[int | None, Query(description="Only jobs older than this id (next page)")] = None,
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
        before_id=before_id,
        limit=limit,
        offset=offset,
    )


@router.get("/jobs/{job_id}", response_model=Job)
def get_job(job_id: int, conn: Conn) -> dict:
    job = queue.get(conn, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job


@router.post("/jobs/{job_id}/retry", response_model=Job)
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


@router.post("/batches", status_code=status.HTTP_201_CREATED, response_model=BatchCreated)
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


@router.get("/batches", response_model=list[BatchSummary])
def list_batches(
    conn: Conn,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    before_id: Annotated[int | None, Query(description="Only batches older than this id (next page)")] = None,
) -> list[dict]:
    return batches.list_batches(conn, limit=limit, offset=offset, before_id=before_id)


@router.get("/batches/{batch_id}", response_model=BatchSummary)
def get_batch(batch_id: int, conn: Conn) -> dict:
    batch = batches.get_batch(conn, batch_id)
    if batch is None:
        raise HTTPException(404, "Batch not found")
    return batch


# --- upload from the browser -----------------------------------------------------------------


def _upload_dir(settings: AppSettings, upload_id: str) -> Path:
    try:
        upload_id = str(uuid.UUID(upload_id))
    except ValueError:
        raise HTTPException(404, "Upload not found") from None
    path = settings.upload_root / upload_id
    if not path.is_dir():
        raise HTTPException(404, "Upload not found")
    return path


ABANDONED_UPLOAD_AGE = "24 hours"


def _remove_abandoned_uploads(conn: Conn, settings: AppSettings) -> None:
    """Delete sessions never completed within a day, and their files. Completed uploads are source data: kept."""
    expired = conn.execute(
        "DELETE FROM upload_sessions WHERE job_id IS NULL AND created_at < now() - %s::interval RETURNING id",
        (ABANDONED_UPLOAD_AGE,),
    ).fetchall()
    # The deleted rows stay locked until commit, so no part request can write into a directory being removed.
    for row in expired:
        shutil.rmtree(settings.upload_root / str(row["id"]), ignore_errors=True)
    conn.commit()


@router.post("/uploads", status_code=status.HTTP_201_CREATED, response_model=UploadSession)
def start_upload(settings: AppSettings, conn: Conn) -> dict:
    """Open an upload session. The browser then sends the files in several smaller requests."""
    _remove_abandoned_uploads(conn, settings)
    upload_id = str(uuid.uuid4())
    (settings.upload_root / upload_id).mkdir(parents=True)
    conn.execute("INSERT INTO upload_sessions (id) VALUES (%s)", (upload_id,))
    conn.commit()
    return {"upload_id": upload_id}


def _lock_upload(conn: Conn, directory: Path, *, completing: bool = False) -> dict:
    # File requests can run together. Completion waits until every file request has finished.
    lock = "UPDATE" if completing else "SHARE"
    query = f"SELECT job_id FROM upload_sessions WHERE id = %s FOR {lock}"
    session = conn.execute(query, (directory.name,)).fetchone()
    if session is None:
        # A directory without a session predates migration 002: adopt it, completed if it was already ingested.
        conn.execute(
            "INSERT INTO upload_sessions (id, job_id) "
            "VALUES (%s, (SELECT min(id) FROM jobs WHERE kind = 'ingest' AND source_path = %s)) "
            "ON CONFLICT DO NOTHING",
            (directory.name, str(directory)),
        )
        session = conn.execute(query, (directory.name,)).fetchone()
    if session is None or not directory.is_dir():  # removed by the abandoned-upload sweep meanwhile
        raise HTTPException(404, "Upload session not found; start a new upload")
    return session


@router.post("/uploads/{upload_id}/files", response_model=UploadResult)
def upload_files(upload_id: str, settings: AppSettings, conn: Conn, files: Annotated[list[UploadFile], File()]) -> dict:
    directory = _upload_dir(settings, upload_id)
    if _lock_upload(conn, directory)["job_id"] is not None:
        raise HTTPException(409, "This upload is already complete")
    limit = settings.max_upload_file_mb * 1024 * 1024
    total_limit = settings.max_upload_total_mb * 1024 * 1024
    # shortcut: parts running in parallel each see the others' finished files only, so the total can overshoot
    # by up to three in-flight parts (3 x 100 MB); an exact cap needs a byte counter serialized per session.
    total = sum(f.stat().st_size for f in directory.glob("*.dcm"))
    saved = 0
    for upload in files:
        name = (upload.filename or "").replace("\\", "/")
        if not name or len(name) > 1024:
            raise HTTPException(400, "Each uploaded file needs a name of at most 1024 characters")
        # Preserve the identity of nested files without ever using client paths on the server.
        target = directory / f"{hashlib.sha256(name.encode()).hexdigest()}.dcm"
        tmp = directory / f".{uuid.uuid4().hex}.tmp"
        counted = target.exists()  # a resent file is already in the total
        digest = hashlib.sha256()
        try:
            written = 0
            with open(tmp, "xb") as out:
                while chunk := upload.file.read(1024 * 1024):
                    written += len(chunk)
                    if written > limit:
                        raise HTTPException(413, f"{name} is larger than {settings.max_upload_file_mb} MB")
                    if not counted and total + written > total_limit:
                        raise HTTPException(413, f"The upload is larger than {settings.max_upload_total_mb} MB")
                    digest.update(chunk)
                    out.write(chunk)
            try:
                os.link(tmp, target)  # publish a complete file, atomically, without replacing an existing one
            except FileExistsError:
                with open(target, "rb") as existing:
                    if hashlib.file_digest(existing, "sha256").digest() != digest.digest():
                        raise HTTPException(409, f"{name} was already uploaded with different contents") from None
            else:
                total += written
        finally:
            tmp.unlink(missing_ok=True)
        saved += 1
    return {"upload_id": upload_id, "saved": saved}


@router.post("/uploads/{upload_id}/complete", status_code=status.HTTP_201_CREATED, response_model=Job)
def complete_upload(upload_id: str, conn: Conn, settings: AppSettings, user: User) -> dict:
    directory = _upload_dir(settings, upload_id)
    session = _lock_upload(conn, directory, completing=True)
    if session["job_id"] is not None:
        return queue.get(conn, session["job_id"])
    if not any(directory.glob("*.dcm")):
        raise HTTPException(400, "Upload at least one file before completing the upload")
    job = queue.enqueue(
        conn, "ingest", submitted_by=user, max_attempts=settings.job_max_attempts, source_path=str(directory)
    )
    conn.execute("UPDATE upload_sessions SET job_id = %s WHERE id = %s", (job["id"], upload_id))
    conn.commit()
    return job


# --- server-side library -----------------------------------------------------------------------


@router.get("/library/directories", response_model=list[LibraryDirectory])
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
