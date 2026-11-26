"""Request and response shapes. They document the API at /api/docs and filter what leaves the server."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Query
from pydantic import BaseModel, Field

# Pages are newest first. A page shorter than `limit` is the last one; otherwise pass its last id as `before_id`.
BeforeId = Annotated[
    int | None, Query(ge=1, le=2**63 - 1, description="Only rows with a smaller id: the last id of the page before")
]
Limit = Annotated[int, Query(ge=1, le=500, description="Page size")]


class SubmitPath(BaseModel):
    path: str = Field(min_length=1, description="DICOM directory, relative to the data folder", examples=["series_1"])


class Job(BaseModel):
    id: int
    path: str
    batch_id: UUID | None
    status: Literal["queued", "running", "done", "failed"]
    error: str | None
    series_id: int | None
    created_at: datetime
    finished_at: datetime | None
    attempt_count: int
    stage: str


class Series(BaseModel):
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


class Located(BaseModel):
    index: int
    z: float
    num_slices: int
    distance_mm: float
    outside: bool  # the requested z is beyond the scanned range; the nearest end slice is shown
