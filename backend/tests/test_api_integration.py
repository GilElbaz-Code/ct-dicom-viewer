"""End-to-end through the API, the queue and the worker, against a real PostgreSQL.

Run with TEST_DATABASE_URL pointing at a server where the user may create databases, e.g.
    TEST_DATABASE_URL=postgresql://ctviewer:ctviewer@localhost:5432/postgres pytest
"""

from __future__ import annotations

import dataclasses
import io
import shutil
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app import repository
from app.errors import JobError
from app.jobs import queue
from app.jobs.handlers import HANDLERS
from app.tools.synthetic import write_series
from app.worker import Worker


def drain(worker: Worker, limit: int = 50) -> int:
    done = 0
    while done < limit and worker.run_once():
        done += 1
    return done


def png_shape(content: bytes) -> tuple[int, ...]:
    return np.array(Image.open(io.BytesIO(content))).shape


def test_interactive_ingest_then_view(env) -> None:
    client, worker, _ = env
    job = client.post("/api/jobs", json={"path": "series_a"}).json()
    assert job["status"] == "queued" and job["kind"] == "ingest"

    assert drain(worker) == 2  # ingest, then the analysis it requested
    job = client.get(f"/api/jobs/{job['id']}").json()
    assert job["status"] == "succeeded", job
    series_id = job["result"]["series_id"]
    analysis = client.get(f"/api/jobs/{job['result']['analysis_job_id']}").json()
    assert analysis["status"] == "succeeded" and analysis["parent_job_id"] == job["id"]

    series = client.get(f"/api/series/{series_id}").json()
    assert series["status"] == "ready"
    assert series["num_slices"] == 40 and series["rows"] == 64
    assert series["slice_thickness"] == 1.0 and series["computed_slice_spacing"] == pytest.approx(2.5)
    assert series["analysis"]["qc"]["slice_gaps"] == 0
    assert "worker_id" not in job and "sagittal_path" not in series["analysis"]  # internals stay server-side

    loc = client.get(f"/api/series/{series_id}/locate", params={"z": 0.4}).json()
    assert loc["z"] == pytest.approx(1.25) and loc["distance_mm"] == pytest.approx(0.85)
    assert loc["index"] == 20 and not loc["outside_volume"]
    assert 0 < loc["sagittal_fraction"] < 1
    far = client.get(f"/api/series/{series_id}/locate", params={"z": 500}).json()
    assert far["index"] == 39 and far["outside_volume"]
    assert client.get(f"/api/series/{series_id}/locate", params={"index": 3}).json()["index"] == 3
    assert client.get(f"/api/series/{series_id}/locate", params={"index": 40}).status_code == 422
    assert client.get(f"/api/series/{series_id}/locate").status_code == 422

    axial = client.get(f"/api/series/{series_id}/axial/20", params={"window": 400, "level": 40})
    assert axial.headers["content-type"] == "image/png" and png_shape(axial.content) == (64, 64)
    sagittal = client.get(f"/api/series/{series_id}/sagittal")
    assert png_shape(sagittal.content) == (21, 64)  # 97.5 mm of z at 5 mm per pixel
    assert client.get(f"/api/series/{series_id}/axial/20", params={"window": 0}).status_code == 422
    assert client.get(f"/api/series/{series_id}/axial/99").status_code == 404


def test_cross_study_queries(env) -> None:
    client, worker, settings = env
    write_series(
        settings.library_root / "thick",
        slices=6,
        size=16,
        slice_spacing=5.0,
        slice_thickness=5.0,
        study_description="HEAD ROUTINE",
        series_description="Head 5mm",
    )
    for path in ("series_a", "thick"):
        client.post("/api/jobs", json={"path": path})
    drain(worker)

    def count(**params) -> int:
        return client.get("/api/series", params=params).json()["total"]

    assert count() == 2
    assert count(max_slice_thickness=1.5) == 1
    assert count(study_description="head routine") == 1
    assert count(q="phantom") == 1
    assert count(modality="ct") == 2


def test_reingest_is_idempotent(env) -> None:
    client, worker, _ = env
    for _ in range(2):
        client.post("/api/jobs", json={"path": "series_a"})
        drain(worker)
    assert client.get("/api/series").json()["total"] == 1


def test_batch_reports_each_row(env) -> None:
    client, worker, _ = env
    csv = b"path\nseries_a\nmissing_dir\n../../etc\n"
    batch = client.post("/api/batches", files={"file": ("batch.csv", csv, "text/csv")}).json()
    assert batch["total_rows"] == 3 and batch["queued"] == 1 and batch["rejected"] == 2
    drain(worker)

    summary = client.get(f"/api/batches/{batch['id']}").json()
    assert summary["done"] and summary["rows_ready"] == 1 and summary["rows_failed"] == 2
    jobs = client.get("/api/jobs", params={"batch_id": batch["id"], "kind": "ingest"}).json()
    by_row = {j["batch_row"]: j for j in jobs}
    assert by_row[1]["status"] == "succeeded"
    assert by_row[2]["error_code"] == "path_not_found"
    assert by_row[3]["error_code"] == "path_not_allowed"
    assert client.post("/api/batches", files={"file": ("x.csv", b"", "text/csv")}).status_code == 400


def test_batch_row_counts_ready_when_series_was_already_being_analyzed(env) -> None:
    client, worker, settings = env
    client.post("/api/jobs", json={"path": "series_a"})
    worker.run_once()  # ingest done; its analysis is queued
    batch = client.post("/api/batches", files={"file": ("b.csv", b"series_a\n", "text/csv")}).json()

    ingest_only = Worker(dataclasses.replace(settings, worker_kinds=("ingest",)))  # a separate worker pool
    try:
        assert ingest_only.run_once()  # re-ingest; the analysis already queued is not duplicated
    finally:
        ingest_only.pool.close()
    assert len(client.get("/api/jobs", params={"kind": "analyze", "status": "queued"}).json()) == 1
    assert client.get(f"/api/batches/{batch['id']}").json()["done"] is False

    drain(worker)
    summary = client.get(f"/api/batches/{batch['id']}").json()
    assert summary["rows_ready"] == 1 and summary["done"]


def test_invalid_series_fails_with_reason_and_can_be_retried(env) -> None:
    client, worker, settings = env
    mixed = shutil.copytree(settings.library_root / "series_a", settings.library_root / "mixed")
    write_series(mixed / "other", slices=3, size=16)
    job = client.post("/api/jobs", json={"path": "mixed"}).json()
    drain(worker)

    failed = client.get(f"/api/jobs/{job['id']}").json()
    assert failed["status"] == "failed" and failed["attempts"] == 1  # data errors are not retried
    assert failed["error_code"] == "multiple_series" and "found 2" in failed["error_message"]

    shutil.rmtree(mixed / "other")  # the user fixes the directory...
    assert client.post(f"/api/jobs/{job['id']}/retry").json()["status"] == "queued"
    drain(worker)
    assert client.get(f"/api/jobs/{job['id']}").json()["status"] == "succeeded"
    assert client.post(f"/api/jobs/{job['id']}/retry").status_code == 409


def test_interactive_path_errors_are_immediate(env) -> None:
    client, _, _ = env
    response = client.post("/api/jobs", json={"path": "/etc"})
    assert response.status_code == 400 and response.json()["code"] == "path_not_allowed"


def test_transient_errors_are_retried_with_backoff(env, monkeypatch) -> None:
    client, worker, _ = env

    def flaky(job, ctx):
        raise OSError("NFS hiccup")

    monkeypatch.setattr(HANDLERS["ingest"], "run", flaky)
    job = client.post("/api/jobs", json={"path": "series_a"}).json()
    drain(worker)
    job = client.get(f"/api/jobs/{job['id']}").json()
    assert job["status"] == "queued" and job["attempts"] == 1 and job["error_code"] == "io_error"
    assert drain(worker) == 0  # backing off: not runnable yet

    with worker.pool.connection() as conn:  # fast-forward the backoff twice
        for _ in range(2):
            conn.execute("UPDATE jobs SET run_after = now() WHERE id = %s", (job["id"],))
            conn.commit()
            drain(worker)
    job = client.get(f"/api/jobs/{job['id']}").json()
    assert job["status"] == "failed" and job["attempts"] == 3


def test_expired_lease_is_reclaimed_and_stale_worker_cannot_finish(env) -> None:
    client, worker, settings = env
    job = client.post("/api/jobs", json={"path": "series_a"}).json()
    with worker.pool.connection() as conn:
        claimed = queue.claim(conn, worker_id="dead-worker", kinds=("ingest",), lease_seconds=60)
        assert claimed["id"] == job["id"]
        assert queue.claim(conn, worker_id="other", kinds=("ingest",), lease_seconds=60) is None
        conn.execute("UPDATE jobs SET locked_until = now() - interval '1 second' WHERE id = %s", (job["id"],))
        reclaimed = queue.claim(conn, worker_id="live-worker", kinds=("ingest",), lease_seconds=60)
        assert reclaimed["id"] == job["id"] and reclaimed["attempts"] == 2
        assert not queue.mark_succeeded(conn, claimed, "dead-worker", {})
        assert queue.record_failure(conn, claimed, "dead-worker", JobError("x", "y")) is None


def test_browser_upload(env, tmp_path: Path) -> None:
    client, worker, _ = env
    source = write_series(tmp_path / "local", slices=8, size=32, series_description="Uploaded")
    upload_id = client.post("/api/uploads").json()["upload_id"]
    files = sorted(source.iterdir())
    for chunk in (files[:5], files[5:]):
        payload = [("files", (f.name, f.read_bytes(), "application/dicom")) for f in chunk]
        assert client.post(f"/api/uploads/{upload_id}/files", files=payload).json()["saved"] == len(chunk)
    job = client.post(f"/api/uploads/{upload_id}/complete").json()
    drain(worker)
    job = client.get(f"/api/jobs/{job['id']}").json()
    assert job["status"] == "succeeded" and job["result"]["num_slices"] == 8
    assert client.post("/api/uploads/not-a-uuid/complete").status_code == 404


def test_library_listing_health_and_stats(env) -> None:
    client, worker, _ = env
    dirs = client.get("/api/library/directories").json()
    assert [d["path"] for d in dirs] == ["series_a"] and dirs[0]["file_count"] == 40
    assert dirs[0]["series_id"] is None
    client.post("/api/jobs", json={"path": "series_a"})
    drain(worker)
    assert client.get("/api/library/directories").json()[0]["series_id"] is not None
    assert client.get("/api/health").json() == {"status": "ok"}
    assert client.get("/api/ready").json()["status"] == "ok"
    stats = client.get("/api/stats").json()
    assert stats["jobs_by_status"] == {"succeeded": 2} and stats["series_by_status"] == {"ready": 1}


def test_ready_reports_database_outage_as_503(env) -> None:
    client, _, _ = env
    client.app.state.pool.close()
    response = client.get("/api/ready")
    assert response.status_code == 503 and response.json()["checks"]["database"].startswith("error")


def test_analysis_output_is_stored_under_the_numeric_series_id(env) -> None:
    client, worker, settings = env
    client.post("/api/jobs", json={"path": "series_a"})
    drain(worker)
    series = client.get("/api/series").json()["items"][0]
    with worker.pool.connection() as conn:  # the path is internal, so read it from the database, not the API
        path = Path(repository.get_series(conn, series["id"])["analysis"]["sagittal_path"])
    assert path.parent == settings.derived_root / str(series["id"])
    assert path.name.startswith("sagittal-") and path.suffix == ".npy"
    assert not list(path.parent.glob("*.tmp.npy"))  # temp files are renamed away


def test_series_reindexed_during_analysis_is_analyzed_again(env, monkeypatch) -> None:
    client, worker, settings = env
    client.post("/api/jobs", json={"path": "series_a"})
    worker.run_once()  # ingest: 40 slices, analysis queued

    handler = HANDLERS["analyze"]
    original_run = handler.run

    def run_then_reindex(job, ctx):
        payload = original_run(job, ctx)  # pixels of the 40-slice series are read...
        for f in sorted((settings.library_root / "series_a").glob("*.dcm"))[:2]:
            f.unlink()  # ...then someone removes two slices and re-indexes before the results are saved
        directory, info = HANDLERS["ingest"].run({"source_path": "series_a"}, ctx)
        with ctx.pool.connection() as conn:
            repository.upsert_series(conn, info, str(directory))
        return payload

    monkeypatch.setattr(handler, "run", run_then_reindex)
    worker.run_once()
    job = client.get("/api/jobs", params={"kind": "analyze"}).json()[0]
    assert job["status"] == "queued" and job["error_code"] == "series_changed"  # stale result not published
    series = client.get(f"/api/series/{job['series_id']}").json()
    assert series["status"] == "indexed" and series["analysis"] is None

    monkeypatch.setattr(handler, "run", original_run)
    with worker.pool.connection() as conn:  # skip the retry backoff
        conn.execute("UPDATE jobs SET run_after = now() WHERE id = %s", (job["id"],))
        conn.commit()
    drain(worker)
    series = client.get(f"/api/series/{job['series_id']}").json()
    assert series["status"] == "ready" and series["num_slices"] == 38
    assert client.get(f"/api/jobs/{job['id']}").json()["attempts"] == 2


def test_locate_rejects_non_finite_locations(env) -> None:
    client, worker, _ = env
    client.post("/api/jobs", json={"path": "series_a"})
    drain(worker)
    series_id = client.get("/api/series").json()["items"][0]["id"]
    for bad in ("nan", "inf", "-inf"):
        assert client.get(f"/api/series/{series_id}/locate", params={"z": bad}).status_code == 422
