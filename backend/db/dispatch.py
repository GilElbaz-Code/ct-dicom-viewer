"""Durable dispatch and recovery transactions. Queue delivery may repeat."""

from collections.abc import Callable

from core.config import settings
from db.pool import pool
from db.repository import settle_attempts

BATCH = 100  # rows per relay pass; a full pass makes the relay go again at once
PUBLISH_RETRY_SECONDS = 5  # how long a failed publication waits before the relay tries Redis again


def publish_due(*, publish: Callable[[list[int]], None]) -> int:
    """Mark a batch published and commit, then publish it to Redis outside the transaction: no lock waits on Redis.

    published_at is set before publishing because claim_job accepts a delivery only for a published (or due) row.
    A relay that dies after the commit leaves the rows published: they are re-sent after DISPATCH_INTERVAL_SECONDS.
    """
    with pool().connection() as conn:
        job_ids = [
            row["job_id"]
            for row in conn.execute(
                "UPDATE job_outbox SET published_at = clock_timestamp(), last_error = NULL, "
                "next_dispatch_at = clock_timestamp() + %s * interval '1 second' "
                "WHERE job_id IN (SELECT o.job_id FROM job_outbox o JOIN jobs j ON j.id = o.job_id "
                "WHERE j.status = 'queued' AND o.next_dispatch_at <= clock_timestamp() "
                "ORDER BY o.next_dispatch_at LIMIT %s FOR UPDATE OF o SKIP LOCKED) RETURNING job_id",
                (settings.dispatch_interval_seconds, BATCH),
            )
        ]
    if not job_ids:
        return 0
    try:
        publish(job_ids)  # one Redis round trip for the batch: it succeeds or fails as a whole
    except Exception as exc:
        with pool().connection() as conn:
            conn.execute(
                "UPDATE job_outbox SET published_at = NULL, last_error = %s, "
                "next_dispatch_at = clock_timestamp() + %s * interval '1 second' WHERE job_id = ANY(%s)",
                (f"{type(exc).__name__}: {exc}", PUBLISH_RETRY_SECONDS, job_ids),
            )
        return 0
    return len(job_ids)


def recover_expired() -> int:
    """Recover interrupted attempts, including failure callbacks lost during a database outage."""
    with pool().connection() as conn:
        # The lease is capped at the hard deadline (claim_job, renew_lease), so this covers timeouts too.
        job_ids = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM jobs WHERE status = 'running' AND lease_expires_at <= clock_timestamp() "
                "ORDER BY lease_expires_at LIMIT %s FOR UPDATE SKIP LOCKED",
                (BATCH,),
            )
        ]
        if job_ids:
            settle_attempts(
                conn=conn,
                job_ids=job_ids,
                error="Worker stopped or lost its lease; the original cause was not recorded",
                retryable=True,
            )
    return len(job_ids)
