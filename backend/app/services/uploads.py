"""Resumable uploads, verified completion, expiry and worker source staging."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import UUID, uuid4

from app.config import settings
from app.db import uploads as repository
from app.services import storage
from app.services.errors import InvalidInput
from app.types import JobRecord
from app.upload_models import UploadManifest


def create(*, manifest: UploadManifest) -> dict[str, Any]:
    upload_id = uuid4()
    repository.create(upload_id=upload_id, manifest=manifest)
    return session(upload_id=upload_id)


def session(*, upload_id: UUID) -> dict[str, Any]:
    row = repository.get(upload_id=upload_id)
    if row["status"] != "complete" and (row["status"] == "expired" or row["timed_out"]):
        raise InvalidInput("Upload session expired; select the directory to start a new session")
    manifest = UploadManifest.model_validate(row["manifest"])
    expires = max(1, min(3600, int((row["expires_at"] - datetime.now(UTC)).total_seconds())))
    files = []
    for index, file in enumerate(manifest.files):
        uploaded = row["status"] == "complete" or storage.is_uploaded(upload_id=upload_id, index=index, file=file)
        form = (
            None
            if row["status"] == "complete"
            else storage.upload_form(upload_id=upload_id, index=index, file=file, expires=expires)
        )
        files.append({**file.model_dump(), "uploaded": uploaded, "form": form})
    return {
        "id": upload_id,
        "name": manifest.name,
        "status": row["status"],
        "expires_at": row["expires_at"],
        "job_id": row["job_id"],
        "files": files,
    }


def seal(manifest: UploadManifest, *, upload_id: UUID) -> None:
    for index, file in enumerate(manifest.files):
        storage.seal_file(upload_id=upload_id, index=index, file=file)


def complete(*, upload_id: UUID) -> JobRecord:
    return repository.complete(upload_id=upload_id, seal=partial(seal, upload_id=upload_id))


def remove_expired(upload_id: UUID, include_sealed: bool) -> None:
    storage.delete_prefix(prefix=f"incoming/{upload_id}/")
    if include_sealed:
        storage.delete_prefix(prefix=f"sealed/{upload_id}/")


def cleanup_expired() -> None:
    repository.cleanup(remove=remove_expired)


@contextmanager
def materialize(*, upload_id: UUID, check: Callable[[], None]) -> Iterator[tuple[Path, dict[str, str]]]:
    """Use bounded temporary disk; database file references point to durable objects."""
    row = repository.get(upload_id=upload_id)
    if row["status"] != "complete":
        raise InvalidInput("Upload has not been completed")
    manifest = UploadManifest.model_validate(row["manifest"])
    with TemporaryDirectory(prefix="ct-study-") as temporary:
        directory = Path(temporary)
        references: dict[str, str] = {}
        for index in range(len(manifest.files)):
            check()
            key = storage.object_key(upload_id=upload_id, index=index, sealed=True)
            reference = f"s3://{settings.s3_bucket}/{key}"
            path = directory / f"{index:04d}.dcm"
            with storage.read_object(reference=reference) as content:
                path.write_bytes(content.getbuffer())
            references[str(path)] = reference
        yield directory, references
