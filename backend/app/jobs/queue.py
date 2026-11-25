"""A durable job queue on a PostgreSQL table.

Life cycle:  queued -> running -> succeeded
                         |
                         +--> queued again (retryable error, with backoff)
                         +--> failed (permanent error, or attempts used up)

Workers claim with `FOR UPDATE SKIP LOCKED`, so any number of workers can poll
without taking the same job. A claim is a lease (`locked_until`): the worker
extends it while working; if the worker dies, the lease runs out and another
worker picks the job up again.
"""

from __future__ import annotations

from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from app.errors import JobError

JOB_COLUMNS = """
    id, kind, status, source_path, series_id, batch_id, batch_row, parent_job_id, submitted_by,
    attempts, max_attempts, run_after, locked_until, worker_id, error_code, error_message, result,
    created_at, started_at, finished_at, updated_at
"""


def enqueue(
    conn: Connection,
    kind: str,
    *,
    submitted_by: str,
    max_attempts: int,
    source_path: str | None = None,
    series_id: int | None = None,
    batch_id: int | None = None,
    batch_row: int | None = None,
    parent_job_id: int | None = None,
) -> dict | None:
    """Insert a queued job. Returns None if the same series already has an active job of this kind."""
    return conn.execute(
        f"""
        INSERT INTO jobs (kind, source_path, series_id, batch_id, batch_row, parent_job_id, submitted_by, max_attempts)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (kind, series_id) WHERE status IN ('queued', 'running') DO NOTHING
        RETURNING {JOB_COLUMNS}
        """,
        (kind, source_path, series_id, batch_id, batch_row, parent_job_id, submitted_by, max_attempts),
    ).fetchone()


def insert_failed(
    conn: Connection,
    kind: str,
    *,
    submitted_by: str,
    error: JobError,
    source_path: str | None = None,
    batch_id: int | None = None,
    batch_row: int | None = None,
) -> dict:
    """Record a job that is invalid on arrival (e.g. a CSV row with a bad path), so the user sees it."""
    return conn.execute(
        f"""
        INSERT INTO jobs (kind, status, source_path, batch_id, batch_row, submitted_by, max_attempts,
                          error_code, error_message, finished_at)
        VALUES (%s, 'failed', %s, %s, %s, %s, 0, %s, %s, now())
        RETURNING {JOB_COLUMNS}
        """,
        (kind, source_path, batch_id, batch_row, submitted_by, error.code, error.message),
    ).fetchone()


def claim(conn: Connection, *, worker_id: str, kinds: tuple[str, ...], lease_seconds: int) -> dict | None:
    """Take the oldest runnable job, or an abandoned one whose lease expired."""
    return conn.execute(
        f"""
        UPDATE jobs SET
            status = 'running',
            attempts = attempts + 1,
            worker_id = %(worker)s,
            locked_until = now() + make_interval(secs => %(lease)s),
            started_at = now(),
            updated_at = now()
        WHERE id = (
            SELECT id FROM jobs
            WHERE kind = ANY(%(kinds)s)
              AND ((status = 'queued' AND run_after <= now())
                   OR (status = 'running' AND locked_until < now()))
            ORDER BY id
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING {JOB_COLUMNS}
        """,
        {"worker": worker_id, "lease": lease_seconds, "kinds": list(kinds)},
    ).fetchone()


def extend_lease(conn: Connection, job_id: int, worker_id: str, lease_seconds: int) -> bool:
    cur = conn.execute(
        """
        UPDATE jobs SET locked_until = now() + make_interval(secs => %s), updated_at = now()
        WHERE id = %s AND worker_id = %s AND status = 'running'
        """,
        (lease_seconds, job_id, worker_id),
    )
    return cur.rowcount == 1


def mark_succeeded(conn: Connection, job: dict, worker_id: str, result: dict[str, Any]) -> bool:
    """False means this worker no longer owns the job (its lease expired and someone else took it)."""
    cur = conn.execute(
        """
        UPDATE jobs SET status = 'succeeded', result = %s, series_id = COALESCE(%s, series_id),
               error_code = NULL, error_message = NULL, locked_until = NULL,
               finished_at = now(), updated_at = now()
        WHERE id = %s AND worker_id = %s AND status = 'running'
        """,
        (Jsonb(result), result.get("series_id"), job["id"], worker_id),
    )
    return cur.rowcount == 1


def backoff_seconds(attempt: int) -> int:
    return min(300, 10 * 2 ** (attempt - 1))  # 10s, 20s, 40s ... capped at 5 min


def record_failure(conn: Connection, job: dict, worker_id: str, error: JobError) -> str | None:
    """Requeue with backoff if the error is retryable and attempts remain, else fail for good.

    Returns the new status, or None if the job is no longer owned by this worker.
    """
    retry = error.retryable and job["attempts"] < job["max_attempts"]
    row = conn.execute(
        """
        UPDATE jobs SET
            status = CASE WHEN %(retry)s THEN 'queued' ELSE 'failed' END,
            run_after = CASE WHEN %(retry)s THEN now() + make_interval(secs => %(delay)s) ELSE run_after END,
            finished_at = CASE WHEN %(retry)s THEN NULL ELSE now() END,
            error_code = %(code)s, error_message = %(message)s,
            locked_until = NULL, updated_at = now()
        WHERE id = %(id)s AND worker_id = %(worker)s AND status = 'running'
        RETURNING status
        """,
        {
            "retry": retry,
            "delay": backoff_seconds(job["attempts"]),
            "code": error.code,
            "message": error.message[:4000],
            "id": job["id"],
            "worker": worker_id,
        },
    ).fetchone()
    return row["status"] if row else None


def retry_failed(conn: Connection, job_id: int, max_attempts: int) -> dict | None:
    """Put a failed job back in the queue (user-initiated)."""
    return conn.execute(
        f"""
        UPDATE jobs SET status = 'queued', attempts = 0, max_attempts = %s, run_after = now(),
               error_code = NULL, error_message = NULL, finished_at = NULL, worker_id = NULL,
               updated_at = now()
        WHERE id = %s AND status = 'failed'
        RETURNING {JOB_COLUMNS}
        """,
        (max_attempts, job_id),
    ).fetchone()


def get(conn: Connection, job_id: int) -> dict | None:
    return conn.execute(f"SELECT {JOB_COLUMNS} FROM jobs WHERE id = %s", (job_id,)).fetchone()


def list_jobs(
    conn: Connection,
    *,
    status: str | None = None,
    kind: str | None = None,
    batch_id: int | None = None,
    series_id: int | None = None,
    submitted_by: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    clauses, params = [], []
    for column, value in (
        ("status", status),
        ("kind", kind),
        ("batch_id", batch_id),
        ("series_id", series_id),
        ("submitted_by", submitted_by),
    ):
        if value is not None:
            clauses.append(f"{column} = %s")
            params.append(value)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return conn.execute(
        f"SELECT {JOB_COLUMNS} FROM jobs {where} ORDER BY id DESC LIMIT %s OFFSET %s",
        (*params, limit, offset),
    ).fetchall()


def stats(conn: Connection) -> dict:
    """Queue health numbers for monitoring: depth, age of the oldest waiting job, recent failures."""
    by_status = {
        r["status"]: r["n"] for r in conn.execute("SELECT status, count(*) AS n FROM jobs GROUP BY status").fetchall()
    }
    row = conn.execute(
        """
        SELECT
          EXTRACT(EPOCH FROM now() - min(created_at) FILTER (WHERE status = 'queued' AND run_after <= now()))
            AS oldest_queued_seconds,
          count(*) FILTER (WHERE status = 'running' AND locked_until < now()) AS expired_leases,
          count(*) FILTER (WHERE status = 'failed' AND finished_at > now() - interval '1 hour') AS failed_last_hour,
          count(*) FILTER (WHERE status = 'succeeded' AND finished_at > now() - interval '1 hour')
            AS succeeded_last_hour
        FROM jobs
        """
    ).fetchone()
    oldest = row["oldest_queued_seconds"]
    return {
        "jobs_by_status": by_status,
        "oldest_queued_seconds": float(oldest) if oldest is not None else None,
        "expired_leases": row["expired_leases"],
        "failed_last_hour": row["failed_last_hour"],
        "succeeded_last_hour": row["succeeded_last_hour"],
    }
