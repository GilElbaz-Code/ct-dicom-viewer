"""Upload bytes go directly to object storage, not through these HTTP routes."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter

from app.api.schemas import Job
from app.services import uploads
from app.types import JobRecord
from app.upload_models import UploadManifest

router = APIRouter(prefix="/api/uploads", tags=["uploads"])


@router.post("", status_code=201)
def create(manifest: UploadManifest) -> dict[str, Any]:
    return uploads.create(manifest=manifest)


@router.get("/{upload_id}")
def session(upload_id: UUID) -> dict[str, Any]:
    return uploads.session(upload_id=upload_id)


@router.post("/{upload_id}/complete", response_model=Job, status_code=202)
def complete(upload_id: UUID) -> JobRecord:
    return uploads.complete(upload_id=upload_id)
