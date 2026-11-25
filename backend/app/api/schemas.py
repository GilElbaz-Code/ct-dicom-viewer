"""Response shapes of the API.

They document every endpoint in /api/docs, and because FastAPI filters the
response through them, internal columns (lease, worker id, server-side paths of
derived files) never reach the client.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel

from app.dicom.imaging import SagittalGeometry

JobStatus = Literal["queued", "running", "succeeded", "failed"]
JobKind = Literal["ingest", "analyze"]
SeriesStatus = Literal["indexed", "ready", "analysis_failed"]


class Job(BaseModel):
    id: int
    kind: JobKind
    status: JobStatus
    source_path: str | None
    series_id: int | None
    batch_id: int | None
    batch_row: int | None
    parent_job_id: int | None
    submitted_by: str
    attempts: int
    max_attempts: int
    run_after: datetime
    error_code: str | None
    error_message: str | None
    result: dict[str, Any] | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    updated_at: datetime


class Batch(BaseModel):
    id: int
    filename: str | None
    submitted_by: str
    total_rows: int
    created_at: datetime


class BatchCreated(Batch):
    queued: int
    rejected: int


class BatchSummary(Batch):
    rows_ready: int
    rows_failed: int
    rows_pending: int
    done: bool


class Series(BaseModel):
    id: int
    series_instance_uid: str
    status: SeriesStatus
    source_path: str
    modality: str | None
    series_description: str | None
    series_number: int | None
    rows: int
    columns: int
    num_slices: int
    pixel_spacing_row: float | None
    pixel_spacing_col: float | None
    slice_thickness: float | None
    spacing_between_slices: float | None
    computed_slice_spacing: float
    z_min: float
    z_max: float
    image_orientation: list[float]
    attributes: dict[str, Any]
    warnings: list[str]
    study_id: int
    study_instance_uid: str
    patient_id: str | None
    study_description: str | None
    study_date: date | None
    created_at: datetime
    updated_at: datetime


class Analysis(BaseModel):
    job_id: int | None
    sagittal_geometry: SagittalGeometry
    qc: dict[str, float | None]
    created_at: datetime


class LastAnalysisJob(BaseModel):
    id: int
    status: JobStatus
    error_code: str | None
    error_message: str | None
    attempts: int
    max_attempts: int
    finished_at: datetime | None


class SlicePosition(BaseModel):
    slice_index: int
    z_position: float
    instance_number: int | None


class SeriesDetail(Series):
    analysis: Analysis | None
    last_analysis_job: LastAnalysisJob | None
    slices: list[SlicePosition]


class SeriesPage(BaseModel):
    items: list[Series]
    total: int


class Located(BaseModel):
    index: int
    z: float
    instance_number: int | None
    num_slices: int
    requested_z: float | None
    distance_mm: float
    outside_volume: bool
    sagittal_fraction: float  # where to draw the line on the sagittal image: 0 = top (z_max), 1 = bottom


class LibraryDirectory(BaseModel):
    path: str
    absolute_path: str
    file_count: int
    series_id: int | None


class UploadSession(BaseModel):
    upload_id: str


class UploadResult(UploadSession):
    saved: int
