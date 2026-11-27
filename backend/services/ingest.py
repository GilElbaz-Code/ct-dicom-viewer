"""The background job, run by the RQ worker: read a DICOM directory, analyze it, save the results.

Problems with the data fail the job at once: another attempt would fail the same way. Database and storage
outages are retryable; PostgreSQL counts attempts and the relay re-dispatches them or recovers the expired lease.
"""

from dataclasses import fields
from pathlib import Path
from uuid import uuid4

from psycopg import InterfaceError, OperationalError
from psycopg_pool import PoolTimeout

from core.config import settings
from core.errors import InvalidInput, LeaseLost
from core.logger import logger
from db import repository
from services import dicom
from services.lease import Lease


def run(job_id: int) -> None:
    claim = repository.claim_job(job_id=job_id, token=uuid4())
    if claim is None:
        return
    with Lease(claim=claim) as lease:
        try:
            directory = resolve_inside_data_dir(raw=claim.path)
            info = dicom.read_series(directory=directory, validate=False)
            lease.check()
            series = {f.name: getattr(info, f.name) for f in fields(info) if f.name != "headers"}
            series["source_path"] = claim.path
            repository.save_series_metadata(claim=claim, series=series)
            dicom.validate_geometry(info=info)
            plane = dicom.sagittal(files=info.files, rows=info.rows, columns=info.columns, check=lease.check)
            lease.check()
            repository.save_analysis_and_finish_job(
                claim=claim, series=series, sagittal=plane.tobytes(), encoding=dicom.SAGITTAL_ENCODING
            )
        except LeaseLost:
            logger.warning("Discarding stale attempt for job %s", job_id)
        except Exception as exc:
            logger.exception("Processing failed for job %s", job_id)
            retryable = is_transient(exc=exc)
            try:
                repository.finish_failed_attempt(claim=claim, error=describe(exc=exc), retryable=retryable)
            except LeaseLost:
                logger.warning("Failure from stale attempt ignored for job %s", job_id)
            # If PostgreSQL is unavailable, let the exception propagate. The relay recovers the lease later.


# Missing or forbidden files stay missing on the next attempt; other OS errors (EIO, a stale NFS handle) may pass.
PERMANENT_OS_ERRORS = (FileNotFoundError, PermissionError, IsADirectoryError, NotADirectoryError)


def is_transient(exc: BaseException) -> bool:
    """Database and storage outages are retried; problems with the data are not."""
    if isinstance(exc, (OperationalError, InterfaceError, PoolTimeout)):
        return True
    return isinstance(exc, OSError) and not isinstance(exc, PERMANENT_OS_ERRORS)


def describe(exc: BaseException) -> str:
    """Our own errors are written for users; anything else is shown with its type."""
    return str(exc) if isinstance(exc, InvalidInput) else f"{type(exc).__name__}: {exc}"


def resolve_inside_data_dir(raw: str) -> Path:
    """Relative paths start at DATA_DIR. '..' and symlinks are resolved first, so nothing outside it can be read."""
    root = settings.data_dir.resolve()
    path = (root / raw.strip()).resolve()
    if not path.is_relative_to(root):
        raise InvalidInput(f"{raw} is outside the data directory")
    if path == root:
        raise InvalidInput("Choose a series folder inside the data directory, not the data directory itself")
    if not path.is_dir():
        raise InvalidInput(f"Directory not found: {raw}")
    return path
