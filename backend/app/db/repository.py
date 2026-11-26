"""Every SQL statement of the app. Each function is one transaction: `with pool().connection()` commits on exit."""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from psycopg import Connection
from psycopg.rows import DictRow

from app.config import settings
from app.db.pool import pool
from app.services.errors import LeaseLost
from app.types import JobRecord, PositionRecord, SagittalRecord, SeriesRecord

JOB_COLUMNS = "id, path, batch_id, status, error, series_id, created_at, finished_at, attempt_count, stage"

# Everything the API shows about a series. The big columns (files, sagittal) are only read where needed.
SERIES_COLUMNS = """
    id, series_uid, study_uid, patient_id, study_description, series_description, modality, pixel_spacing,
    slice_thickness, spacing_between_slices, slice_spacing, rows, columns, source_path, updated_at,
    sagittal IS NOT NULL AS analysis_available,
    cardinality(z_positions) AS num_slices, z_positions[1] AS z_min, z_positions[cardinality(z_positions)] AS z_max
"""


# --- jobs ---------------------------------------------------------------------------------------


def create_jobs(paths: list[str], batch_id: UUID | None) -> list[JobRecord]:
    with pool().connection() as conn:
        rows = conn.execute(
            f"INSERT INTO jobs (path, batch_id) SELECT unnest(%s::text[]), %s RETURNING {JOB_COLUMNS}",
            (paths, batch_id),
        ).fetchall()
        conn.execute(
            "INSERT INTO job_outbox(job_id) SELECT unnest(%s::bigint[])",
            ([row["id"] for row in rows],),
        )
        return rows


def list_jobs(batch_id: UUID | None, limit: int = 200) -> list[JobRecord]:
    with pool().connection() as conn:
        return conn.execute(
            f"SELECT {JOB_COLUMNS} FROM jobs WHERE %(batch)s::uuid IS NULL OR batch_id = %(batch)s "
            "ORDER BY id DESC LIMIT %(limit)s",
            {"batch": batch_id, "limit": limit},
        ).fetchall()


@dataclass(frozen=True, slots=True)
class Claim:
    job_id: int
    token: UUID
    path: str


def claim_job(*, job_id: int, token: UUID) -> Claim | None:
    """Only a queued job can start. Recovery, not delivery, requeues expired attempts."""
    with pool().connection() as conn:
        row = conn.execute(
            "UPDATE jobs SET status = 'running', stage = 'headers', attempt_count = attempt_count + 1, "
            "attempt_token = %(token)s, attempt_started_at = clock_timestamp(), "
            "lease_expires_at = clock_timestamp() + %(lease)s * interval '1 second', error = NULL "
            "WHERE id = %(id)s AND status = 'queued' AND attempt_count < %(max)s "
            "AND EXISTS (SELECT 1 FROM job_outbox WHERE job_id = jobs.id AND "
            "(published_at IS NOT NULL OR next_dispatch_at <= clock_timestamp())) RETURNING path",
            {"id": job_id, "token": token, "lease": settings.lease_seconds, "max": settings.max_attempts},
        ).fetchone()
    return Claim(job_id=job_id, token=token, path=row["path"]) if row else None


def require_lease(*, conn: Connection[DictRow], claim: Claim) -> None:
    """Lock the job before writes so a stale attempt cannot race recovery or completion."""
    row = conn.execute(
        "SELECT id FROM jobs WHERE id = %s AND status = 'running' AND attempt_token = %s "
        "AND lease_expires_at > clock_timestamp() "
        "AND attempt_started_at + %s * interval '1 second' > clock_timestamp() FOR UPDATE",
        (claim.job_id, claim.token, settings.job_timeout_seconds),
    ).fetchone()
    if row is None:
        raise LeaseLost("The processing lease expired or belongs to another attempt")


def renew_lease(*, claim: Claim) -> bool:
    with pool().connection() as conn:
        return (
            conn.execute(
                "UPDATE jobs SET lease_expires_at = clock_timestamp() + %(lease)s * interval '1 second' "
                "WHERE id = %(id)s AND attempt_token = %(token)s AND status = 'running' "
                "AND lease_expires_at > clock_timestamp() "
                "AND attempt_started_at + %(timeout)s * interval '1 second' > clock_timestamp() RETURNING id",
                {
                    "id": claim.job_id,
                    "token": claim.token,
                    "lease": settings.lease_seconds,
                    "timeout": settings.job_timeout_seconds,
                },
            ).fetchone()
            is not None
        )


def finish_failed_attempt(*, claim: Claim, error: str, retryable: bool) -> None:
    with pool().connection() as conn:
        require_lease(conn=conn, claim=claim)
        row = conn.execute(
            "SELECT attempt_count FROM jobs WHERE id = %s",
            (claim.job_id,),
        ).fetchone()
        retry = retryable and row["attempt_count"] < settings.max_attempts
        conn.execute(
            "UPDATE jobs SET status = %s, error = %s, attempt_token = NULL, lease_expires_at = NULL, "
            "finished_at = CASE WHEN %s THEN NULL ELSE clock_timestamp() END WHERE id = %s",
            ("queued" if retry else "failed", error, retry, claim.job_id),
        )
        if retry:
            conn.execute(
                "UPDATE job_outbox SET published_at = NULL, next_dispatch_at = clock_timestamp() + "
                "%s * interval '1 second' WHERE job_id = %s",
                (settings.retry_delay_seconds * 2 ** (row["attempt_count"] - 1), claim.job_id),
            )


def save_series_metadata(*, claim: Claim, series: dict[str, Any]) -> int:
    """Commit searchable headers before analysis; preserve an existing successful result."""
    with pool().connection() as conn:
        require_lease(conn=conn, claim=claim)
        series_id = conn.execute(
            """
            INSERT INTO series (series_uid, study_uid, patient_id, study_description, series_description, modality,
                                pixel_spacing, slice_thickness, spacing_between_slices, slice_spacing, rows, columns,
                                z_positions, files, source_path)
            VALUES (%(series_uid)s, %(study_uid)s, %(patient_id)s, %(study_description)s, %(series_description)s,
                    %(modality)s, %(pixel_spacing)s, %(slice_thickness)s, %(spacing_between_slices)s,
                    %(slice_spacing)s, %(rows)s, %(columns)s, %(z_positions)s, %(files)s,
                    %(source_path)s)
            ON CONFLICT (series_uid) DO UPDATE SET
                study_uid = EXCLUDED.study_uid, patient_id = EXCLUDED.patient_id,
                study_description = EXCLUDED.study_description, series_description = EXCLUDED.series_description,
                modality = EXCLUDED.modality, pixel_spacing = EXCLUDED.pixel_spacing,
                slice_thickness = EXCLUDED.slice_thickness, spacing_between_slices = EXCLUDED.spacing_between_slices,
                slice_spacing = EXCLUDED.slice_spacing, rows = EXCLUDED.rows, columns = EXCLUDED.columns,
                z_positions = EXCLUDED.z_positions, files = EXCLUDED.files,
                source_path = EXCLUDED.source_path, updated_at = now()
            RETURNING id
            """,
            series,
        ).fetchone()["id"]
        conn.execute(
            "UPDATE jobs SET series_id = %s, stage = 'analysis' WHERE id = %s",
            (series_id, claim.job_id),
        )
        return series_id


def save_analysis_and_finish_job(*, claim: Claim, series_id: int, sagittal: bytes) -> None:
    """Publish the output and successful job outcome atomically."""
    with pool().connection() as conn:
        require_lease(conn=conn, claim=claim)
        conn.execute(
            "UPDATE series SET sagittal = %s, updated_at = now() WHERE id = %s",
            (sagittal, series_id),
        )
        conn.execute(
            "UPDATE jobs SET status = 'done', stage = 'complete', error = NULL, finished_at = now(), "
            "attempt_token = NULL, lease_expires_at = NULL WHERE id = %s",
            (claim.job_id,),
        )


# --- series -------------------------------------------------------------------------------------


def search_series(max_slice_thickness: float | None, description: str | None, limit: int = 500) -> list[SeriesRecord]:
    """E.g. every series thinner than 1.5 mm, or every study whose description contains a word."""
    with pool().connection() as conn:
        return conn.execute(
            f"""
            SELECT {SERIES_COLUMNS} FROM series
            WHERE (%(thickness)s::float8 IS NULL OR slice_thickness < %(thickness)s)
              AND (%(text)s::text IS NULL
                   OR study_description ILIKE %(pattern)s OR series_description ILIKE %(pattern)s)
            ORDER BY id DESC LIMIT %(limit)s
            """,
            {"thickness": max_slice_thickness, "text": description, "pattern": f"%{description}%", "limit": limit},
        ).fetchall()


def get_series(series_id: int) -> SeriesRecord | None:
    with pool().connection() as conn:
        return conn.execute(f"SELECT {SERIES_COLUMNS} FROM series WHERE id = %s", (series_id,)).fetchone()


def get_positions(series_id: int) -> PositionRecord | None:
    with pool().connection() as conn:
        return conn.execute("SELECT z_positions, slice_spacing FROM series WHERE id = %s", (series_id,)).fetchone()


def get_slice_file(series_id: int, index: int) -> str | None:
    with pool().connection() as conn:
        row = conn.execute("SELECT files[%s] AS file FROM series WHERE id = %s", (index + 1, series_id)).fetchone()
    return row["file"] if row else None  # Postgres arrays start at 1; out of range gives NULL


def get_sagittal(series_id: int) -> SagittalRecord | None:
    with pool().connection() as conn:
        return conn.execute(
            "SELECT sagittal, cardinality(z_positions) AS num_slices, rows, pixel_spacing, slice_spacing "
            "FROM series WHERE id = %s",
            (series_id,),
        ).fetchone()
