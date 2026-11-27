"""Every SQL statement of the app. Each function is one transaction: `with pool().connection()` commits on exit."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg import Connection
from psycopg.rows import DictRow

from core.config import settings
from core.errors import LeaseLost
from core.types import AxialRecord, JobRecord, PositionRecord, SagittalRecord, SeriesRecord
from db.pool import pool

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
    """Jobs and their outbox rows commit together."""
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


def list_jobs(batch_id: UUID | None, before_id: int | None, limit: int) -> list[JobRecord]:
    """Newest first. Keyset pagination: the next page starts below the last id seen, so rows never shift."""
    filters = {"batch_id = %(batch)s": batch_id, "id < %(before)s": before_id}
    with pool().connection() as conn:
        return conn.execute(
            f"SELECT {JOB_COLUMNS} FROM jobs WHERE {where(filters)} ORDER BY id DESC LIMIT %(limit)s",
            {"batch": batch_id, "before": before_id, "limit": limit},
        ).fetchall()


def where(filters: dict[str, object]) -> str:
    """AND the constant SQL conditions whose value is given. Each query plan then fits the filters actually used,
    where `(%s IS NULL OR col = %s)` would leave psycopg's prepared (generic) plan unable to use the indexes."""
    return " AND ".join(condition for condition, value in filters.items() if value is not None) or "TRUE"


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
            # The lease never outlives the hard deadline, so an expired lease alone signals every interrupted attempt.
            "lease_expires_at = clock_timestamp() + least(%(lease)s, %(timeout)s) * interval '1 second', error = NULL "
            "WHERE id = %(id)s AND status = 'queued' AND attempt_count < %(max)s "
            "AND EXISTS (SELECT 1 FROM job_outbox WHERE job_id = jobs.id AND "
            "(published_at IS NOT NULL OR next_dispatch_at <= clock_timestamp())) RETURNING path",
            {
                "id": job_id,
                "token": token,
                "lease": settings.lease_seconds,
                "timeout": settings.job_timeout_seconds,
                "max": settings.max_attempts,
            },
        ).fetchone()
    return Claim(job_id=job_id, token=token, path=row["path"]) if row else None


def require_lease(*, conn: Connection[DictRow], claim: Claim) -> None:
    """Lock the job before writes so a stale attempt cannot race recovery or completion."""
    row = conn.execute(
        "SELECT id FROM jobs WHERE id = %s AND status = 'running' AND attempt_token = %s "
        "AND lease_expires_at > clock_timestamp() FOR UPDATE",
        (claim.job_id, claim.token),
    ).fetchone()
    if row is None:
        raise LeaseLost("The processing lease expired or belongs to another attempt")


def renew_lease(*, claim: Claim) -> bool:
    """Extend a live lease, but never past the attempt's hard deadline: there the next renewal fails."""
    with pool().connection() as conn:
        return (
            conn.execute(
                "UPDATE jobs SET lease_expires_at = least(clock_timestamp() + %(lease)s * interval '1 second', "
                "attempt_started_at + %(timeout)s * interval '1 second') "
                "WHERE id = %(id)s AND attempt_token = %(token)s AND status = 'running' "
                "AND lease_expires_at > clock_timestamp() RETURNING id",
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
        settle_attempts(conn=conn, job_ids=[claim.job_id], error=error, retryable=retryable)


def settle_attempts(*, conn: Connection[DictRow], job_ids: list[int], error: str, retryable: bool) -> None:
    """End failed attempts in one statement: a retryable failure is requeued with exponential backoff until
    MAX_ATTEMPTS, anything else fails the job. A job that can no longer run leaves the outbox."""
    conn.execute(
        """
        WITH target AS (
            SELECT id, attempt_count, %(retryable)s AND attempt_count < %(max)s AS retry
            FROM jobs WHERE id = ANY(%(ids)s)
        ), settled AS (
            UPDATE jobs j SET
                status = CASE WHEN t.retry THEN 'queued' ELSE 'failed' END,
                error = CASE WHEN %(retryable)s AND NOT t.retry
                             THEN format('Gave up after %%s attempts: %%s', t.attempt_count, %(error)s::text)
                             ELSE %(error)s::text END,
                attempt_token = NULL, lease_expires_at = NULL,
                finished_at = CASE WHEN t.retry THEN NULL ELSE clock_timestamp() END
            FROM target t WHERE j.id = t.id
            RETURNING j.id, t.retry, t.attempt_count
        ), finished AS (
            DELETE FROM job_outbox o USING settled s WHERE o.job_id = s.id AND NOT s.retry
        )
        UPDATE job_outbox o SET published_at = NULL, next_dispatch_at = clock_timestamp()
            + %(delay)s * power(2, s.attempt_count - 1) * interval '1 second'
        FROM settled s WHERE o.job_id = s.id AND s.retry
        """,
        {
            "ids": job_ids,
            "error": error,
            "retryable": retryable,
            "max": settings.max_attempts,
            "delay": settings.retry_delay_seconds,
        },
    )


# Every header column of a series row, in schema order. Constants, so building SQL from them is safe.
SERIES_FIELDS = (
    "series_uid", "study_uid", "patient_id", "study_description", "series_description", "modality", "pixel_spacing",
    "slice_thickness", "spacing_between_slices", "slice_spacing", "rows", "columns", "z_positions", "files",
    "source_path", "sagittal", "sagittal_encoding",
)  # fmt: skip


def upsert_series(*, conn: Connection[DictRow], series: dict[str, Any], replace_analyzed: bool) -> int:
    """Write one complete version of a series: headers, slice files and plane always come from the same read."""
    updates = ", ".join(f"{field} = EXCLUDED.{field}" for field in SERIES_FIELDS[1:])
    row = conn.execute(
        f"INSERT INTO series ({', '.join(SERIES_FIELDS)}) VALUES ({', '.join(f'%({f})s' for f in SERIES_FIELDS)}) "
        f"ON CONFLICT (series_uid) DO UPDATE SET {updates}, updated_at = now() "
        f"{'' if replace_analyzed else 'WHERE series.sagittal IS NULL'} RETURNING id",
        series,
    ).fetchone()
    if row is None:  # an analyzed version stays whole until a successful analysis replaces it
        row = conn.execute("SELECT id FROM series WHERE series_uid = %s", (series["series_uid"],)).fetchone()
    return row["id"]


def save_series_metadata(*, claim: Claim, series: dict[str, Any]) -> int:
    """Make a new series searchable before analysis. An analyzed series keeps its current version meanwhile."""
    with pool().connection() as conn:
        require_lease(conn=conn, claim=claim)
        series_id = upsert_series(
            conn=conn, series={**series, "sagittal": None, "sagittal_encoding": None}, replace_analyzed=False
        )
        conn.execute(
            "UPDATE jobs SET series_id = %s, stage = 'analysis' WHERE id = %s",
            (series_id, claim.job_id),
        )
        return series_id


def save_analysis_and_finish_job(*, claim: Claim, series: dict[str, Any], sagittal: bytes, encoding: str) -> None:
    """Swap in the whole new version (headers, files, plane and its encoding) and mark the job done, atomically.

    Concurrent jobs for one SeriesInstanceUID each write a complete version; the last to finish wins.
    """
    with pool().connection() as conn:
        require_lease(conn=conn, claim=claim)
        series_id = upsert_series(
            conn=conn, series={**series, "sagittal": sagittal, "sagittal_encoding": encoding}, replace_analyzed=True
        )
        conn.execute(
            "UPDATE jobs SET status = 'done', stage = 'complete', error = NULL, finished_at = now(), series_id = %s, "
            "attempt_token = NULL, lease_expires_at = NULL WHERE id = %s",
            (series_id, claim.job_id),
        )
        conn.execute("DELETE FROM job_outbox WHERE job_id = %s", (claim.job_id,))  # done: nothing left to dispatch


# --- series -------------------------------------------------------------------------------------


def search_series(
    max_slice_thickness: float | None, description: str | None, before_id: int | None, limit: int
) -> list[SeriesRecord]:
    """E.g. every series thinner than 1.5 mm, or every study whose description contains a word. Newest first."""
    filters = {
        "slice_thickness < %(thickness)s": max_slice_thickness,
        "(study_description ILIKE %(pattern)s OR series_description ILIKE %(pattern)s)": description,
        "id < %(before)s": before_id,
    }
    with pool().connection() as conn:
        return conn.execute(
            f"SELECT {SERIES_COLUMNS} FROM series WHERE {where(filters)} ORDER BY id DESC LIMIT %(limit)s",
            {
                "thickness": max_slice_thickness,
                "pattern": f"%{like_literal(description)}%",
                "before": before_id,
                "limit": limit,
            },
        ).fetchall()


def like_literal(text: str | None) -> str:
    """Escape LIKE's wildcards (backslash is PostgreSQL's default escape character)."""
    return (text or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def get_series(series_id: int) -> SeriesRecord | None:
    with pool().connection() as conn:
        return conn.execute(f"SELECT {SERIES_COLUMNS} FROM series WHERE id = %s", (series_id,)).fetchone()


# The version check is part of each image or location query, so the data and its version come from one snapshot.
# Without a version (a client that does not send one) every row is current.
CURRENT = "coalesce(updated_at = %(version)s::timestamptz, true) AS current"


def get_positions(series_id: int, version: datetime | None) -> PositionRecord | None:
    with pool().connection() as conn:
        return conn.execute(
            f"SELECT z_positions, slice_spacing, {CURRENT} FROM series WHERE id = %(id)s",
            {"id": series_id, "version": version},
        ).fetchone()


def get_axial(series_id: int, index: int, version: datetime | None) -> AxialRecord | None:
    """Everything one axial image needs, in one query. Postgres arrays start at 1; out of range gives file NULL."""
    with pool().connection() as conn:
        return conn.execute(
            "SELECT sagittal IS NOT NULL AS analysis_available, pixel_spacing, rows, columns, "
            f"files[%(subscript)s] AS file, {CURRENT} FROM series WHERE id = %(id)s",
            {"subscript": index + 1, "id": series_id, "version": version},
        ).fetchone()


def get_sagittal(series_id: int, version: datetime | None) -> SagittalRecord | None:
    with pool().connection() as conn:
        return conn.execute(
            "SELECT sagittal, sagittal_encoding, cardinality(z_positions) AS num_slices, rows, pixel_spacing, "
            f"slice_spacing, {CURRENT} FROM series WHERE id = %(id)s",
            {"id": series_id, "version": version},
        ).fetchone()
