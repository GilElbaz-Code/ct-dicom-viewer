"""Durable dispatch and recovery transactions. Queue delivery may repeat."""

from collections.abc import Callable

from app.config import settings
from app.db.pool import pool


def publish_due(*, publish: Callable[[int], None]) -> int:
    """Lock only outbox rows; a crashed relay rolls back publication bookkeeping."""
    count = 0
    with pool().connection() as conn:
        rows = conn.execute(
            "SELECT o.job_id FROM job_outbox o JOIN jobs j ON j.id = o.job_id "
            "WHERE j.status = 'queued' AND o.next_dispatch_at <= clock_timestamp() "
            "ORDER BY o.next_dispatch_at LIMIT 100 FOR UPDATE OF o SKIP LOCKED"
        ).fetchall()
        for row in rows:
            try:
                publish(row["job_id"])
            except Exception as exc:
                conn.execute(
                    "UPDATE job_outbox SET last_error = %s, next_dispatch_at = clock_timestamp() + "
                    "interval '5 seconds' WHERE job_id = %s",
                    (f"{type(exc).__name__}: {exc}", row["job_id"]),
                )
                # Redis is likely down: stop here so a hung queue holds this transaction for one timeout, not 100.
                # The remaining rows stay due for the next pass; the failed one moves behind them.
                break
            conn.execute(
                "UPDATE job_outbox SET published_at = clock_timestamp(), last_error = NULL, "
                "next_dispatch_at = clock_timestamp() + %s * interval '1 second' WHERE job_id = %s",
                (settings.dispatch_interval_seconds, row["job_id"]),
            )
            count += 1
    return count


def recover_expired() -> int:
    """Recover interrupted attempts, including failure callbacks lost during a database outage."""
    with pool().connection() as conn:
        rows = conn.execute(
            "SELECT id, attempt_count FROM jobs WHERE status = 'running' AND "
            "(lease_expires_at <= clock_timestamp() OR attempt_started_at + "
            "%s * interval '1 second' <= clock_timestamp()) "
            "ORDER BY lease_expires_at LIMIT 100 FOR UPDATE SKIP LOCKED",
            (settings.job_timeout_seconds,),
        ).fetchall()
        for row in rows:
            retry = row["attempt_count"] < settings.max_attempts
            reason = "Worker stopped or lost its lease; the original cause was not recorded"
            if not retry:
                reason = f"Gave up after {row['attempt_count']} attempts: {reason}"
            conn.execute(
                "UPDATE jobs SET status = %s, error = %s, attempt_token = NULL, lease_expires_at = NULL, "
                "finished_at = CASE WHEN %s THEN NULL ELSE clock_timestamp() END WHERE id = %s",
                ("queued" if retry else "failed", reason, retry, row["id"]),
            )
            if retry:
                conn.execute(
                    "UPDATE job_outbox SET published_at = NULL, next_dispatch_at = clock_timestamp() + "
                    "%s * interval '1 second' WHERE job_id = %s",
                    (settings.retry_delay_seconds * 2 ** (row["attempt_count"] - 1), row["id"]),
                )
    return len(rows)
