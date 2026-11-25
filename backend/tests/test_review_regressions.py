"""Integration regressions: real transactions and files; no browser or external research storage."""

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest

from app import repository
from app.config import get_settings
from app.jobs import handlers as handler_module
from app.jobs import queue
from app.worker import Worker
from tests.test_api_integration import drain


def test_stale_analysis_cannot_replace_published_pixels(env, monkeypatch):
    client, worker, settings = env
    client.post("/api/jobs", json={"path": "series_a"})
    worker.run_once()
    original = handler_module.analyze_series
    replacement = Worker(settings)
    calls = 0

    def interrupted_analysis(*args, **kwargs):
        nonlocal calls
        calls += 1
        output = original(*args, **kwargs)
        if calls == 1:
            output.sagittal.fill(-1234)
            # A's pixel computation pauses; its lease expires. B finishes before A writes its file.
            with worker.pool.connection() as conn:
                conn.execute("UPDATE jobs SET locked_until = now() - interval '1 second' WHERE kind = 'analyze'")
            assert replacement.run_once()
        else:
            output.sagittal.fill(567)
        return output

    monkeypatch.setattr(handler_module, "analyze_series", interrupted_analysis)
    try:
        worker.run_once()
    finally:
        replacement.pool.close()
    series_id = client.get("/api/series").json()["items"][0]["id"]
    with worker.pool.connection() as conn:
        series = repository.get_series(conn, series_id)
    assert series["status"] == "ready"
    assert series["last_analysis_job"]["status"] == "succeeded"
    assert series["last_analysis_job"]["attempts"] == 2
    assert np.all(np.load(series["analysis"]["sagittal_path"]) == 567)


def _wait_until_a_query_waits_for_a_lock(pool, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with pool.connection() as conn:
            waiting = conn.execute(
                "SELECT count(*) AS n FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            ).fetchone()["n"]
        if waiting:
            return
        time.sleep(0.02)
    raise AssertionError("no query is waiting for a lock")


def test_skip_locked_passes_over_a_job_another_worker_is_claiming(env):
    client, worker, settings = env
    ids = {client.post("/api/jobs", json={"path": "series_a"}).json()["id"] for _ in range(2)}
    claim = {"kinds": ("ingest",), "lease_seconds": settings.job_lease_seconds}
    with worker.pool.connection() as first:  # claimed, not yet committed: the row stays locked
        a = queue.claim(first, worker_id="a", **claim)
        with worker.pool.connection() as second:
            second.execute("SET LOCAL lock_timeout = '2s'")  # without SKIP LOCKED this would wait, then fail
            b = queue.claim(second, worker_id="b", **claim)
    assert {a["id"], b["id"]} == ids


def test_completion_waits_for_a_part_in_flight(env):
    client, worker, settings = env
    upload_id = client.post("/api/uploads").json()["upload_id"]  # still empty
    with worker.pool.connection() as part, ThreadPoolExecutor(max_workers=1) as executor:
        # What a part request holds while it writes files.
        part.execute("SELECT 1 FROM upload_sessions WHERE id = %s FOR SHARE", (upload_id,))
        completion = executor.submit(client.post, f"/api/uploads/{upload_id}/complete")
        _wait_until_a_query_waits_for_a_lock(worker.pool)
        (settings.upload_root / upload_id / "a.dcm").write_bytes(b"x")  # the part's file lands
        part.commit()
        # Completion looked at the directory only after the part finished, so it saw the file (not 400 "empty").
        assert completion.result(timeout=10).status_code == 201


def test_upload_retries_and_concurrent_completion_are_idempotent(env):
    client, worker, settings = env
    upload_id = client.post("/api/uploads").json()["upload_id"]
    source = sorted((settings.library_root / "series_a").glob("*.dcm"))
    payload = [("files", (f"nested/{f.name}", f.read_bytes(), "application/dicom")) for f in source]
    for _ in range(2):
        assert client.post(f"/api/uploads/{upload_id}/files", files=payload).status_code == 200
    assert len(list((settings.upload_root / upload_id).glob("*.dcm"))) == len(source)
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: client.post(f"/api/uploads/{upload_id}/complete"), range(2)))
    assert all(response.status_code == 201 for response in responses)
    assert responses[0].json()["id"] == responses[1].json()["id"]
    assert client.post(f"/api/uploads/{upload_id}/files", files=payload).status_code == 409
    assert drain(worker) == 2
    series = client.get("/api/series").json()["items"][0]
    assert series["num_slices"] == len(source) and series["status"] == "ready"


def test_upload_names_are_distinct_and_conflicting_retries_fail(env):
    client, _, settings = env
    upload_id = client.post("/api/uploads").json()["upload_id"]
    url = f"/api/uploads/{upload_id}/files"
    for name in ("one/slice.dcm", "two/slice.dcm"):
        assert client.post(url, files={"files": (name, b"original")}).status_code == 200
    assert client.post(url, files={"files": ("one/slice.dcm", b"changed")}).status_code == 409
    files = list((settings.upload_root / upload_id).iterdir())
    assert len(files) == 2 and all(f.read_bytes() == b"original" for f in files)


def test_empty_upload_cannot_be_completed(env):
    client, _, _ = env
    upload_id = client.post("/api/uploads").json()["upload_id"]
    assert client.post(f"/api/uploads/{upload_id}/complete").status_code == 400
    # Python accepts this spelling of a UUID and Postgres does not: still a 400, not a 500.
    assert client.post(f"/api/uploads/urn:uuid:{upload_id}/complete").status_code == 400
    assert client.get("/api/jobs").json() == []


def test_series_detail_includes_positions_for_local_navigation(env):
    client, worker, _ = env
    client.post("/api/jobs", json={"path": "series_a"})
    drain(worker)
    series_id = client.get("/api/series").json()["items"][0]["id"]
    series = client.get(f"/api/series/{series_id}").json()
    assert len(series["slices"]) == series["num_slices"]
    for index in (0, 20, 39):
        located = client.get(f"/api/series/{series_id}/locate", params={"index": index}).json()
        assert located["z"] == series["slices"][index]["z_position"]
        assert "file_path" not in series["slices"][index]


@pytest.mark.parametrize("view", ["axial/20", "sagittal"])
def test_image_rendering_releases_database_connection(env, monkeypatch, view):
    from app.api import series as series_api

    client, worker, _ = env
    client.post("/api/jobs", json={"path": "series_a"})
    drain(worker)
    series_id = client.get("/api/series").json()["items"][0]["id"]
    original = series_api.encode_png

    def encode(image):
        stats = client.app.state.pool.get_stats()
        assert stats["pool_available"] == stats["pool_size"]
        return original(image)

    monkeypatch.setattr(series_api, "encode_png", encode)
    assert client.get(f"/api/series/{series_id}/{view}").status_code == 200


def test_pagination_reaches_older_jobs_and_batches(env):
    client, _, _ = env
    for _ in range(3):
        client.post("/api/batches", files={"file": ("paths.csv", b"path\nseries_a\n")})
    for resource in ("jobs", "batches"):
        all_rows = client.get(f"/api/{resource}").json()
        pages = [client.get(f"/api/{resource}", params={"limit": 1, "offset": i}).json()[0] for i in range(3)]
        assert [row["id"] for row in pages] == [row["id"] for row in all_rows[:3]]
        # Keyset pages do not shift when newer rows arrive between requests.
        params = {"limit": 1, "before_id": all_rows[0]["id"]}
        second_page = client.get(f"/api/{resource}", params=params).json()
        client.post("/api/batches", files={"file": ("paths.csv", b"path\nseries_a\n")})
        assert client.get(f"/api/{resource}", params=params).json() == second_page
        assert second_page[0]["id"] == all_rows[1]["id"]


def test_reanalysis_removes_superseded_sagittal_files(env):
    client, worker, settings = env
    for _ in range(2):  # index and analyze, then re-index: a second analysis publishes a new file
        client.post("/api/jobs", json={"path": "series_a"})
        drain(worker)
    series_id = client.get("/api/series").json()["items"][0]["id"]
    with worker.pool.connection() as conn:
        published = Path(repository.get_series(conn, series_id)["analysis"]["sagittal_path"])
    assert list((settings.derived_root / str(series_id)).iterdir()) == [published]
    assert client.get(f"/api/series/{series_id}/sagittal").status_code == 200


def test_abandoned_uploads_are_removed_and_completed_ones_kept(env):
    client, worker, settings = env
    abandoned = client.post("/api/uploads").json()["upload_id"]
    completed = client.post("/api/uploads").json()["upload_id"]
    for upload_id in (abandoned, completed):
        client.post(f"/api/uploads/{upload_id}/files", files={"files": ("a.dcm", b"x")})
    client.post(f"/api/uploads/{completed}/complete")
    with worker.pool.connection() as conn:
        conn.execute("UPDATE upload_sessions SET created_at = now() - interval '25 hours'")
    client.post("/api/uploads")
    assert not (settings.upload_root / abandoned).exists()
    assert client.post(f"/api/uploads/{abandoned}/complete").status_code == 404
    assert (settings.upload_root / completed).is_dir()
    assert client.post(f"/api/uploads/{completed}/complete").status_code == 201


def test_total_upload_size_is_capped_and_resent_files_do_not_count(env, monkeypatch):
    client, _, _ = env
    monkeypatch.setenv("MAX_UPLOAD_TOTAL_MB", "1")
    get_settings.cache_clear()
    upload_id = client.post("/api/uploads").json()["upload_id"]
    url = f"/api/uploads/{upload_id}/files"
    first = ("files", ("a.dcm", b"x" * 600_000))
    assert client.post(url, files=[first]).status_code == 200
    assert client.post(url, files=[("files", ("b.dcm", b"y" * 600_000))]).status_code == 413
    assert client.post(url, files=[first]).status_code == 200  # a retry of a stored file is still fine


def test_uploads_started_before_upload_sessions_existed_are_adopted(env):
    client, worker, _ = env
    in_flight = client.post("/api/uploads").json()["upload_id"]
    done = client.post("/api/uploads").json()["upload_id"]
    for upload_id in (in_flight, done):
        client.post(f"/api/uploads/{upload_id}/files", files={"files": ("a.dcm", b"x")})
    job = client.post(f"/api/uploads/{done}/complete").json()
    with worker.pool.connection() as conn:
        conn.execute("DELETE FROM upload_sessions")  # what an upload directory from before migration 002 looks like
    assert client.post(f"/api/uploads/{in_flight}/files", files={"files": ("b.dcm", b"y")}).status_code == 200
    assert client.post(f"/api/uploads/{in_flight}/complete").status_code == 201
    assert client.post(f"/api/uploads/{done}/files", files={"files": ("b.dcm", b"y")}).status_code == 409
    assert client.post(f"/api/uploads/{done}/complete").json()["id"] == job["id"]
