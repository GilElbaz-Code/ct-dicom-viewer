"""End to end through the HTTP API against a real PostgreSQL.

Skipped unless TEST_DATABASE_URL is set: the test drops and recreates the tables in that database.
The RQ queue is replaced by one that runs each task immediately, so no Redis or worker is needed.
"""

import os
import shutil
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from redis import ConnectionError as RedisConnectionError

from app.config import settings
from app.db import dispatch, repository
from app.db.pool import pool
from app.main import app
from app.services import dicom, ingest
from app.services.errors import LeaseLost

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="set TEST_DATABASE_URL to a disposable PostgreSQL database")


def submit(client, path, **kwargs):
    response = client.post(path, **kwargs)
    pending = []
    dispatch.publish_due(publish=pending.append)
    for job_id in pending:
        ingest.run(job_id=job_id)
    return response


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_url", DATABASE_URL)
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    pool.cache_clear()
    with pool().connection() as conn:
        conn.execute("DROP TABLE IF EXISTS job_outbox, upload_sessions, jobs, series CASCADE")
        conn.execute((Path(__file__).parents[1] / "schema.sql").read_text())
    yield TestClient(app)
    pool().close()
    pool.cache_clear()


def test_index_search_and_view_a_series(client, make_series):
    make_series("chest", n=5)
    submitted = submit(client, "/api/jobs", json={"path": "chest"}).json()
    [job] = client.get("/api/jobs").json()
    assert (job["id"], job["status"]) == (submitted["id"], "done")
    series_id = job["series_id"]

    assert [s["id"] for s in client.get("/api/series", params={"max_slice_thickness": 1.5}).json()] == [series_id]
    assert client.get("/api/series", params={"max_slice_thickness": 0.5}).json() == []
    assert len(client.get("/api/series", params={"description": "chest"}).json()) == 1

    series = client.get(f"/api/series/{series_id}").json()
    assert (series["num_slices"], series["z_min"], series["z_max"]) == (5, -100, -90)
    assert client.get(f"/api/series/{series_id}/locate", params={"z": -95.2}).json()["index"] == 2

    for url in (f"/api/series/{series_id}/axial/2", f"/api/series/{series_id}/sagittal?window=1500&level=-600"):
        response = client.get(url)
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
    assert client.get(f"/api/series/{series_id}/axial/9").status_code == 404
    assert client.get(f"/api/series/{series_id}/locate").status_code == 422


def test_batch_rows_fail_one_by_one_with_a_reason(client, make_series):
    make_series("good")
    created = submit(client, "/api/batches", files={"file": ("batch.csv", b"path\ngood\nmissing\n../outside\n")}).json()
    batch_ids = {job["batch_id"] for job in created}
    assert len(created) == 3 and len(batch_ids) == 1

    by_path = {job["path"]: job for job in client.get("/api/jobs", params={"batch_id": batch_ids.pop()}).json()}
    assert by_path["good"]["status"] == "done"
    # Data errors fail on the first attempt, with the reason as written (no "Gave up after 3 attempts").
    assert (by_path["missing"]["status"], by_path["missing"]["error"]) == ("failed", "Directory not found: missing")
    assert by_path["../outside"]["error"] == "../outside is outside the data directory"


def test_a_duplicate_delivery_of_a_finished_job_is_skipped(client, make_series, tmp_path):
    make_series("chest")
    job_id = submit(client, "/api/jobs", json={"path": "chest"}).json()["id"]
    shutil.rmtree(tmp_path / "chest")  # if the job ran again, it would now fail with "Directory not found"

    ingest.run(job_id)
    [job] = client.get("/api/jobs").json()
    assert (job["status"], job["error"]) == ("done", None)


def test_queue_outage_keeps_accepted_jobs_queued(client):
    submitted = client.post("/api/jobs", json={"path": "chest"}).json()

    def unavailable(job_id):
        raise RedisConnectionError("Connection refused")

    assert dispatch.publish_due(publish=unavailable) == 0
    assert submitted["status"] == "queued"
    assert client.get("/api/jobs").json()[0]["status"] == "queued"
    with pool().connection() as conn:
        row = conn.execute("SELECT last_error FROM job_outbox WHERE job_id = %s", (submitted["id"],)).fetchone()
    assert "Connection refused" in row["last_error"]


def test_analysis_failure_preserves_searchable_metadata(client, make_series, monkeypatch):
    make_series("chest")

    def fail_analysis(**kwargs):
        raise ValueError("Corrupt pixel data")

    monkeypatch.setattr(dicom, "sagittal", fail_analysis)
    submit(client, "/api/jobs", json={"path": "chest"})
    [job] = client.get("/api/jobs").json()
    assert job["status"] == "failed"
    assert "Corrupt pixel data" in job["error"]
    [series] = client.get("/api/series", params={"max_slice_thickness": 1.5}).json()
    assert series["id"] == job["series_id"]
    assert series["analysis_available"] is False
    assert series["patient_id"] == "ANON01"
    for suffix in ("sagittal", "axial/0"):
        response = client.get(f"/api/series/{series['id']}/{suffix}")
        assert response.status_code == 422
        assert "Analysis is not available" in response.json()["detail"]


def test_failed_reanalysis_preserves_previous_result(client, make_series, monkeypatch):
    make_series("chest")
    submit(client, "/api/jobs", json={"path": "chest"})
    [series] = client.get("/api/series").json()
    image_url = f"/api/series/{series['id']}/sagittal"
    original = client.get(image_url).content

    def fail_analysis(**kwargs):
        raise ValueError("Decoder unavailable")

    monkeypatch.setattr(dicom, "sagittal", fail_analysis)
    submit(client, "/api/jobs", json={"path": "chest"})
    assert client.get("/api/jobs").json()[0]["status"] == "failed"
    assert client.get(image_url).content == original
    assert client.get("/api/series").json()[0]["analysis_available"] is True


def test_metadata_migration_preserves_existing_series(client, make_series):
    make_series("chest")
    submit(client, "/api/jobs", json={"path": "chest"})
    with pool().connection() as conn:
        conn.execute("ALTER TABLE series ALTER COLUMN sagittal SET NOT NULL")
        migration = Path(__file__).parents[1] / "migrations/001_metadata_without_analysis.sql"
        conn.execute(migration.read_text(encoding="utf-8"))
    [series] = client.get("/api/series").json()
    assert series["analysis_available"] is True
    assert client.get(f"/api/series/{series['id']}/sagittal").status_code == 200


def expire(job_id):
    with pool().connection() as conn:
        conn.execute("UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = %s", (job_id,))


def make_due(job_id):
    with pool().connection() as conn:
        conn.execute(
            "UPDATE job_outbox SET next_dispatch_at = now() - interval '1 second' WHERE job_id = %s", (job_id,)
        )


def test_only_one_concurrent_delivery_can_claim_a_job(client):
    from concurrent.futures import ThreadPoolExecutor

    job_id = client.post("/api/jobs", json={"path": "chest"}).json()["id"]
    with ThreadPoolExecutor(max_workers=4) as executor:
        claims = list(executor.map(lambda _: repository.claim_job(job_id=job_id, token=uuid4()), range(4)))
    assert sum(claim is not None for claim in claims) == 1
    assert client.get("/api/jobs").json()[0]["attempt_count"] == 1


def test_expired_attempt_cannot_write_after_recovery(client):
    job_id = client.post("/api/jobs", json={"path": "chest"}).json()["id"]
    stale = repository.claim_job(job_id=job_id, token=uuid4())
    expire(job_id)
    assert repository.renew_lease(claim=stale) is False
    assert dispatch.recover_expired() == 1
    make_due(job_id)
    current = repository.claim_job(job_id=job_id, token=uuid4())
    assert current is not None
    with pytest.raises(LeaseLost):
        repository.finish_failed_attempt(claim=stale, error="late failure", retryable=False)
    with pytest.raises(LeaseLost):
        repository.save_series_metadata(claim=stale, series={})
    with pytest.raises(LeaseLost):
        repository.save_analysis_and_finish_job(claim=stale, series_id=1, sagittal=b"bad")
    assert repository.renew_lease(claim=current) is True
    assert client.get("/api/jobs").json()[0]["status"] == "running"


def test_recovery_stops_after_three_interrupted_attempts(client):
    job_id = client.post("/api/jobs", json={"path": "chest"}).json()["id"]
    for _ in range(3):
        make_due(job_id)
        assert repository.claim_job(job_id=job_id, token=uuid4()) is not None
        expire(job_id)
        dispatch.recover_expired()
    [job] = client.get("/api/jobs").json()
    assert job["status"] == "failed"
    assert "Gave up after 3 attempts" in job["error"]
    assert "original cause was not recorded" in job["error"]
    assert repository.claim_job(job_id=job_id, token=uuid4()) is None


def test_lost_queue_message_is_republished_without_consuming_attempts(client):
    job_id = client.post("/api/jobs", json={"path": "chest"}).json()["id"]
    published = []
    assert dispatch.publish_due(publish=published.append) == 1
    make_due(job_id)
    assert dispatch.publish_due(publish=published.append) == 1
    assert published == [job_id, job_id]
    assert client.get("/api/jobs").json()[0]["attempt_count"] == 0


def test_retry_delay_is_respected_even_by_duplicate_deliveries(client):
    job_id = client.post("/api/jobs", json={"path": "chest"}).json()["id"]
    claim = repository.claim_job(job_id=job_id, token=uuid4())
    repository.finish_failed_attempt(claim=claim, error="Storage unavailable", retryable=True)
    assert repository.claim_job(job_id=job_id, token=uuid4()) is None
    assert dispatch.publish_due(publish=lambda _: pytest.fail("retry was dispatched too soon")) == 0


def test_hard_deadline_cannot_be_extended_by_heartbeats(client):
    job_id = client.post("/api/jobs", json={"path": "chest"}).json()["id"]
    claim = repository.claim_job(job_id=job_id, token=uuid4())
    with pool().connection() as conn:
        conn.execute("UPDATE jobs SET attempt_started_at = now() - interval '31 minutes' WHERE id = %s", (job_id,))
    assert repository.renew_lease(claim=claim) is False
    assert dispatch.recover_expired() == 1


def test_dispatch_rollback_can_repeat_publication_safely(client):
    job_id = client.post("/api/jobs", json={"path": "chest"}).json()["id"]
    published = []

    def crash_after_publish(identifier):
        published.append(identifier)
        raise KeyboardInterrupt("relay died after enqueue")

    with pytest.raises(KeyboardInterrupt):
        dispatch.publish_due(publish=crash_after_publish)
    dispatch.publish_due(publish=published.append)
    assert published == [job_id, job_id]
