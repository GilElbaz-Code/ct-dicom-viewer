"""Shared service records, independent of HTTP response models."""

from datetime import datetime
from typing import Literal, TypedDict
from uuid import UUID

type JobStatus = Literal["queued", "running", "done", "failed"]


class JobRecord(TypedDict):
    id: int
    path: str
    batch_id: UUID | None
    status: JobStatus
    error: str | None
    series_id: int | None
    created_at: datetime
    finished_at: datetime | None
    attempt_count: int
    stage: str


class SeriesRecord(TypedDict):
    id: int
    series_uid: str
    study_uid: str
    patient_id: str | None
    study_description: str | None
    series_description: str | None
    modality: str | None
    pixel_spacing: list[float] | None
    slice_thickness: float | None
    spacing_between_slices: float | None
    slice_spacing: float
    num_slices: int
    rows: int
    columns: int
    z_min: float
    z_max: float
    source_path: str
    updated_at: datetime
    analysis_available: bool


class LocatedRecord(TypedDict):
    index: int
    z: float
    num_slices: int
    distance_mm: float
    outside: bool


class PositionRecord(TypedDict):
    z_positions: list[float]
    slice_spacing: float
    current: bool  # the requested version (updated_at), or any when none was given


class AxialRecord(TypedDict):
    analysis_available: bool
    pixel_spacing: list[float] | None
    rows: int
    columns: int
    file: str | None
    current: bool


class SagittalRecord(TypedDict):
    sagittal: bytes | None
    sagittal_encoding: str | None  # a key of dicom.SAGITTAL_ENCODINGS
    num_slices: int
    rows: int
    pixel_spacing: list[float] | None
    slice_spacing: float
    current: bool
