"""Upload lifecycle transactions; completion and job dispatch commit together."""

from collections.abc import Callable
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.db.pool import pool
from app.db.repository import JOB_COLUMNS
from app.services.errors import InvalidInput, NotFound
from app.types import JobRecord
from app.upload_models import UploadManifest


def create(*, upload_id: UUID, manifest: UploadManifest) -> None:
    with pool().connection() as conn:
        conn.execute(
            "INSERT INTO upload_sessions(id, name, manifest) VALUES (%s, %s, %s)",
            (upload_id, manifest.name, Jsonb(manifest.model_dump())),
        )


def get(*, upload_id: UUID) -> dict[str, Any]:
    with pool().connection() as conn:
        row = conn.execute(
            "SELECT *, expires_at <= clock_timestamp() AS timed_out FROM upload_sessions WHERE id = %s",
            (upload_id,),
        ).fetchone()
    if row is None:
        raise NotFound("Upload session not found")
    return row


def complete(*, upload_id: UUID, seal: Callable[[UploadManifest], None]) -> JobRecord:
    """Serialize completion/expiry. Retry after a lost response returns the original accepted job."""
    with pool().connection() as conn:
        row = conn.execute(
            "SELECT *, expires_at <= clock_timestamp() AS timed_out FROM upload_sessions WHERE id = %s FOR UPDATE",
            (upload_id,),
        ).fetchone()
        if row is None:
            raise NotFound("Upload session not found")
        if row["status"] == "complete":
            return conn.execute(f"SELECT {JOB_COLUMNS} FROM jobs WHERE id = %s", (row["job_id"],)).fetchone()
        if row["status"] == "expired" or row["timed_out"]:
            raise InvalidInput("Upload session expired; select the directory to start a new session")
        seal(UploadManifest.model_validate(row["manifest"]))
        job = conn.execute(
            f"INSERT INTO jobs(path, upload_id) VALUES (%s, %s) RETURNING {JOB_COLUMNS}",
            (f"Upload: {row['name']}", upload_id),
        ).fetchone()
        conn.execute("INSERT INTO job_outbox(job_id) VALUES (%s)", (job["id"],))
        conn.execute(
            "UPDATE upload_sessions SET status = 'complete', job_id = %s WHERE id = %s", (job["id"], upload_id)
        )
        return job


def cleanup(*, remove: Callable[[UUID, bool], None]) -> None:
    """Keep tombstones until the signed URLs have expired; retry failed object deletion on the next pass."""
    with pool().connection() as conn:
        rows = conn.execute(
            "SELECT id, status FROM upload_sessions WHERE expires_at < clock_timestamp() "
            "AND cleaned_at IS NULL ORDER BY expires_at LIMIT 10 FOR UPDATE SKIP LOCKED"
        ).fetchall()
        for row in rows:
            remove(row["id"], row["status"] != "complete")
            conn.execute(
                "UPDATE upload_sessions SET cleaned_at = clock_timestamp(), "
                "status = CASE WHEN status = 'complete' THEN status ELSE 'expired' END WHERE id = %s",
                (row["id"],),
            )
