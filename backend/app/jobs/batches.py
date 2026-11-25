"""CSV batches: one batch row, one ingest job per CSV row."""

from __future__ import annotations

from psycopg import Connection

from app.config import Settings
from app.errors import SeriesValidationError
from app.jobs import queue
from app.sources import CsvRow, resolve_source_path


def create_batch(conn: Connection, rows: list[CsvRow], *, filename: str | None, user: str, settings: Settings) -> dict:
    batch = conn.execute(
        "INSERT INTO batches (filename, submitted_by, total_rows) VALUES (%s, %s, %s) RETURNING *",
        (filename, user, len(rows)),
    ).fetchone()
    queued = rejected = 0
    for row in rows:
        try:
            path = resolve_source_path(row.path, settings)
        except SeriesValidationError as error:
            # Rejected rows still become jobs, so the user sees each row's outcome in one place.
            queue.insert_failed(
                conn,
                "ingest",
                submitted_by=user,
                error=error,
                source_path=row.path,
                batch_id=batch["id"],
                batch_row=row.row_number,
            )
            rejected += 1
            continue
        queue.enqueue(
            conn,
            "ingest",
            submitted_by=user,
            max_attempts=settings.job_max_attempts,
            source_path=str(path),
            batch_id=batch["id"],
            batch_row=row.row_number,
        )
        queued += 1
    return {**batch, "queued": queued, "rejected": rejected}


def _counts(conn: Connection, batch_ids: list[int]) -> dict[int, dict]:
    """Outcome per CSV row: its ingest job, then the state of the series it produced.

    The series state (not the batch's own analyze job) decides "ready", because a
    series that was already being analyzed is not analyzed a second time.
    """
    out: dict[int, dict] = {bid: {"ready": 0, "failed": 0, "pending": 0} for bid in batch_ids}
    if not batch_ids:
        return out
    for r in conn.execute(
        """
        SELECT j.batch_id,
               CASE WHEN j.status = 'failed' OR s.status = 'analysis_failed' THEN 'failed'
                    WHEN s.status = 'ready' THEN 'ready'
                    ELSE 'pending' END AS outcome,
               count(*) AS n
        FROM jobs j LEFT JOIN series s ON s.id = j.series_id
        WHERE j.batch_id = ANY(%s) AND j.kind = 'ingest'
        GROUP BY 1, 2
        """,
        (batch_ids,),
    ).fetchall():
        out[r["batch_id"]][r["outcome"]] = r["n"]
    return out


def _summarize(batch: dict, counts: dict) -> dict:
    return {
        **batch,
        "rows_ready": counts["ready"],
        "rows_failed": counts["failed"],
        "rows_pending": counts["pending"],
        "done": counts["pending"] == 0,
    }


def list_batches(conn: Connection, *, limit: int = 50, offset: int = 0, before_id: int | None = None) -> list[dict]:
    batches = conn.execute(
        "SELECT * FROM batches WHERE %(before)s::bigint IS NULL OR id < %(before)s "
        "ORDER BY id DESC LIMIT %(limit)s OFFSET %(offset)s",
        {"before": before_id, "limit": limit, "offset": offset},
    ).fetchall()
    counts = _counts(conn, [b["id"] for b in batches])
    return [_summarize(b, counts[b["id"]]) for b in batches]


def get_batch(conn: Connection, batch_id: int) -> dict | None:
    batch = conn.execute("SELECT * FROM batches WHERE id = %s", (batch_id,)).fetchone()
    if batch is None:
        return None
    return _summarize(batch, _counts(conn, [batch_id])[batch_id])
