"""What each job kind does.

A handler has two steps:
  run(job, ctx)               slow work outside any transaction (reading files, computing,
                              writing derived files)
  persist(conn, job, payload) fast writes inside the transaction that also marks the job succeeded
  after_commit(payload, ctx)  housekeeping once this worker's result is committed (never on a lost lease)

Keeping the slow part outside the transaction means no locks are held while a
1 GB series is read, and the database never sees half of a result.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from psycopg import Connection
from psycopg_pool import ConnectionPool

from app import repository
from app.config import Settings
from app.dicom.analysis import AnalysisOutput, analyze_series
from app.dicom.series import SeriesInfo, read_series_headers
from app.errors import JobError, SeriesValidationError
from app.jobs import queue
from app.sources import resolve_source_path


@dataclass(frozen=True)
class JobContext:
    settings: Settings
    pool: ConnectionPool


class Handler(Protocol):
    def run(self, job: dict, ctx: JobContext) -> Any: ...
    def persist(self, conn: Connection, job: dict, payload: Any, ctx: JobContext) -> dict: ...
    def after_commit(self, payload: Any, ctx: JobContext) -> None: ...
    def on_final_failure(self, conn: Connection, job: dict, error: JobError) -> None: ...


class IngestHandler:
    """Read the headers of one directory, store the metadata, then request the analysis."""

    def run(self, job: dict, ctx: JobContext) -> tuple[Path, SeriesInfo]:
        directory = resolve_source_path(job["source_path"], ctx.settings)
        return directory, read_series_headers(directory)

    def persist(self, conn: Connection, job: dict, payload: tuple[Path, SeriesInfo], ctx: JobContext) -> dict:
        directory, info = payload
        series_id = repository.upsert_series(conn, info, str(directory))
        analysis = queue.enqueue(
            conn,
            "analyze",
            series_id=series_id,
            parent_job_id=job["id"],
            batch_id=job["batch_id"],
            batch_row=job["batch_row"],
            submitted_by=job["submitted_by"],
            max_attempts=ctx.settings.job_max_attempts,
        )
        return {
            "series_id": series_id,
            "series_instance_uid": info.series_instance_uid,
            "num_slices": info.num_slices,
            "skipped_files": info.skipped_files,
            "warnings": info.warnings,
            "analysis_job_id": analysis["id"] if analysis else None,
        }

    def after_commit(self, payload: tuple[Path, SeriesInfo], ctx: JobContext) -> None:
        pass

    def on_final_failure(self, conn: Connection, job: dict, error: JobError) -> None:
        pass


class AnalyzeHandler:
    """One pass over the pixels: mid-sagittal plane for the viewer and QC numbers."""

    def run(self, job: dict, ctx: JobContext) -> tuple[dict, AnalysisOutput, Path]:
        with ctx.pool.connection() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            series = repository.get_series(conn, job["series_id"]) if job["series_id"] else None
            instances = repository.list_instances(conn, series["id"]) if series else []
        if not series or not instances:
            raise SeriesValidationError("series_not_found", "The series was deleted or re-indexed")
        pixel_spacing = (
            (series["pixel_spacing_row"], series["pixel_spacing_col"]) if series["pixel_spacing_row"] else None
        )
        output = analyze_series(
            [i["file_path"] for i in instances],
            [i["z_position"] for i in instances],
            rows=series["rows"],
            columns=series["columns"],
            orientation=series["image_orientation"],
            pixel_spacing=pixel_spacing,
            slice_spacing=series["computed_slice_spacing"],
        )
        # The numeric database id names the folder: header values (UIDs) never become path components.
        out_dir = ctx.settings.derived_root / str(int(series["id"]))
        # Immutable per execution, including manual retries (which reset the attempt counter).
        # A worker that loses its lease may still finish writing, but cannot replace a published file.
        sagittal_path = out_dir / f"sagittal-{uuid.uuid4().hex}.npy"
        _atomic_save(sagittal_path, output.sagittal)
        return series, output, sagittal_path

    def persist(self, conn: Connection, job: dict, payload: tuple[dict, AnalysisOutput, Path], ctx: JobContext) -> dict:
        series, output, sagittal_path = payload
        # Re-indexed while we were reading pixels? Then these results describe the old slice list.
        # Retry (not fail): the next attempt analyzes the current slices.
        current = conn.execute("SELECT updated_at FROM series WHERE id = %s FOR UPDATE", (series["id"],)).fetchone()
        if current is None or current["updated_at"] != series["updated_at"]:
            raise JobError(
                "series_changed", "The series was re-indexed during analysis; analyzing again", retryable=True
            )
        repository.save_analysis(
            conn, series["id"], job["id"], str(sagittal_path), output.geometry.to_dict(), output.qc
        )
        return {"series_id": series["id"], "qc": output.qc}

    def after_commit(self, payload: tuple[dict, AnalysisOutput, Path], ctx: JobContext) -> None:
        """Remove files older than the one just published: superseded results, stale workers' output, temp files.

        Only older ones: a newer file may belong to a later analysis that has not committed yet.
        """
        published = payload[2]
        cutoff = published.stat().st_mtime_ns
        for path in published.parent.glob("*.npy"):  # includes the hidden .tmp.npy files
            with contextlib.suppress(FileNotFoundError):
                if path != published and path.stat().st_mtime_ns < cutoff:
                    path.unlink()

    def on_final_failure(self, conn: Connection, job: dict, error: JobError) -> None:
        if job["series_id"]:
            repository.set_series_status(conn, job["series_id"], "analysis_failed")


def _atomic_save(path: Path, array: np.ndarray) -> None:
    """Write to a temp file then rename, so readers never see a half-written array."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.stem}-{uuid.uuid4().hex}.tmp.npy")  # unique: two attempts never share it
    try:
        np.save(tmp, array)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


HANDLERS: dict[str, Handler] = {"ingest": IngestHandler(), "analyze": AnalyzeHandler()}
