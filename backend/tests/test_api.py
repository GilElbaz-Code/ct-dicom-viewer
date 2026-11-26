"""End to end through the HTTP API against a real PostgreSQL.

Skipped unless TEST_DATABASE_URL is set: the test drops and recreates the tables in that database.
The RQ queue is replaced by one that runs each task immediately, so no Redis or worker is needed.
"""

import io
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from redis import ConnectionError as RedisConnectionError

from app.config import settings
from app.db import dispatch, repository
from app.db.pool import pool
from app.errors import LeaseLost
from app.main import app
from app.services import dicom, ingest
from tests.conftest import write_slice

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
        conn.execute("DROP TABLE IF EXISTS idempotency_keys, job_outbox, jobs, series CASCADE")
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


def test_axial_images_keep_physical_proportions_for_non_square_pixels(client, tmp_path):
    directory = tmp_path / "wide"
    directory.mkdir()
    for z in range(3):  # rows 0.5 mm apart, columns 1.0 mm apart: 8 x 8 pixels cover 8 mm wide by 4 mm tall
        write_slice(directory / f"{z}.dcm", z=z, pixel_spacing=(0.5, 1.0))
    submit(client, "/api/jobs", json={"path": "wide"})
    series_id = client.get("/api/jobs").json()[0]["series_id"]
    png = client.get(f"/api/series/{series_id}/axial/1").content
    assert Image.open(io.BytesIO(png)).size == (16, 8)  # (width, height): twice as wide as tall


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


def all_pages(client, url, limit, **params):
    """Follow before_id until a short page, as the UI's "Load older" button does."""
    rows, cursor = [], {}
    while True:
        page = client.get(url, params={**params, **cursor, "limit": limit}).json()
        rows += page
        if len(page) < limit:
            return rows
        cursor = {"before_id": page[-1]["id"]}


def test_every_job_and_series_is_reachable_page_by_page(client, make_series, tmp_path):
    csv = "path\n" + "".join(f"missing_{i}\n" for i in range(7))
    created = client.post("/api/batches", files={"file": ("batch.csv", csv.encode())}).json()
    client.post("/api/jobs", json={"path": "other"})
    jobs = all_pages(client, "/api/jobs", limit=3, batch_id=created[0]["batch_id"])
    assert [job["id"] for job in jobs] == sorted((job["id"] for job in created), reverse=True)  # each row once

    for i in range(4):
        directory = tmp_path / f"s{i}"
        directory.mkdir()
        for z in range(3):
            write_slice(directory / f"{z}.dcm", z=z, series_uid=f"1.2.826.0.9.{i}")
        submit(client, "/api/jobs", json={"path": f"s{i}"})
    series = all_pages(client, "/api/series", limit=3)
    assert len({s["id"] for s in series}) == len(series) == 4
    assert client.get("/api/jobs", params={"limit": 501}).status_code == 422
    assert client.get("/api/series", params={"limit": 0}).status_code == 422


def test_a_duplicate_delivery_of_a_finished_job_is_skipped(client, make_series, tmp_path):
    make_series("chest")
    job_id = submit(client, "/api/jobs", json={"path": "chest"}).json()["id"]
    shutil.rmtree(tmp_path / "chest")  # if the job ran again, it would now fail with "Directory not found"

    ingest.run(job_id)
    [job] = client.get("/api/jobs").json()
    assert (job["status"], job["error"]) == ("done", None)


def test_a_repeated_idempotency_key_returns_the_original_job(client):
    first = client.post("/api/jobs", json={"path": "chest"}, headers={"Idempotency-Key": "k1"})
    again = client.post("/api/jobs", json={"path": "chest"}, headers={"Idempotency-Key": "k1"})
    assert first.status_code == again.status_code == 201
    assert again.json()["id"] == first.json()["id"]
    assert len(client.get("/api/jobs").json()) == 1


def test_an_idempotency_key_reused_for_another_request_is_rejected(client):
    client.post("/api/jobs", json={"path": "chest"}, headers={"Idempotency-Key": "k1"})
    response = client.post("/api/jobs", json={"path": "head"}, headers={"Idempotency-Key": "k1"})
    assert response.status_code == 422
    assert "different request" in response.json()["detail"]
    assert len(client.get("/api/jobs").json()) == 1


def test_a_repeated_batch_returns_the_same_jobs_and_batch(client):
    csv = {"file": ("batch.csv", b"path\na\nb\n")}
    first = client.post("/api/batches", files=csv, headers={"Idempotency-Key": "batch-1"}).json()
    again = client.post("/api/batches", files=csv, headers={"Idempotency-Key": "batch-1"}).json()
    assert [job["id"] for job in again] == [job["id"] for job in first]
    assert again[0]["batch_id"] == first[0]["batch_id"]
    assert len(client.get("/api/jobs").json()) == 2


def test_concurrent_requests_with_one_key_create_one_job(client):
    # Eight threads race on one key; PostgreSQL makes the losers wait for the winner's commit, then replay it.
    def create(_):
        return repository.create_jobs(paths=["chest"], batch_id=None, idempotency_key="race")[0]["id"]

    with ThreadPoolExecutor(max_workers=8) as threads:
        ids = set(threads.map(create, range(8)))
    assert len(ids) == 1
    assert len(client.get("/api/jobs").json()) == 1


def test_expired_idempotency_keys_are_deleted_so_the_key_can_be_reused(client):
    first = client.post("/api/jobs", json={"path": "chest"}, headers={"Idempotency-Key": "k1"}).json()
    with pool().connection() as conn:
        conn.execute("UPDATE idempotency_keys SET created_at = now() - interval '25 hours'")
    assert repository.delete_expired_idempotency_keys() == 1
    again = client.post("/api/jobs", json={"path": "chest"}, headers={"Idempotency-Key": "k1"}).json()
    assert again["id"] != first["id"]


def stored_version(series_id):
    """(slice count, plane rows, files) as saved: the plane must have one row per stored slice file."""
    with pool().connection() as conn:
        row = conn.execute(
            "SELECT cardinality(files) AS n, length(sagittal) / (2 * rows) AS plane_rows, files "
            "FROM series WHERE id = %s",
            (series_id,),
        ).fetchone()
    return row["n"], row["plane_rows"], row["files"]


def test_failed_reanalysis_keeps_the_previous_version_whole(client, make_series, monkeypatch):
    directory = make_series("chest", n=5)
    submit(client, "/api/jobs", json={"path": "chest"})
    write_slice(directory / "000.dcm", z=-87.5, value=50)  # a sixth slice, same SeriesInstanceUID

    def fail_analysis(**kwargs):
        raise ValueError("Decoder unavailable")

    original = dicom.sagittal
    monkeypatch.setattr(dicom, "sagittal", fail_analysis)
    submit(client, "/api/jobs", json={"path": "chest"})
    assert client.get("/api/jobs").json()[0]["status"] == "failed"
    [series] = client.get("/api/series").json()
    assert (series["num_slices"], series["analysis_available"]) == (5, True)  # the old headers, files and plane
    assert stored_version(series["id"])[:2] == (5, 5)
    assert client.get(f"/api/series/{series['id']}/axial/5").status_code == 404  # the new slice is not mixed in

    monkeypatch.setattr(dicom, "sagittal", original)
    submit(client, "/api/jobs", json={"path": "chest"})
    assert client.get(f"/api/series/{series['id']}").json()["num_slices"] == 6
    assert stored_version(series["id"])[:2] == (6, 6)
    assert client.get(f"/api/series/{series['id']}/axial/5").status_code == 200


def test_concurrent_jobs_for_one_series_never_mix_their_files_and_plane(client, make_series, monkeypatch):
    make_series("first", n=5)
    make_series("second", n=7)  # the same SeriesInstanceUID, other files
    first = client.post("/api/jobs", json={"path": "first"}).json()["id"]
    second = client.post("/api/jobs", json={"path": "second"}).json()["id"]
    dispatch.publish_due(publish=lambda _: None)
    original = dicom.sagittal
    interleaved = []

    def second_job_runs_meanwhile(**kwargs):
        if not interleaved:  # the first job is between saving its headers and saving its plane
            interleaved.append(True)
            ingest.run(job_id=second)
        return original(**kwargs)

    monkeypatch.setattr(dicom, "sagittal", second_job_runs_meanwhile)
    ingest.run(job_id=first)
    assert {job["status"] for job in client.get("/api/jobs").json()} == {"done"}
    [series] = client.get("/api/series").json()
    n, plane_rows, files = stored_version(series["id"])
    assert (series["num_slices"], n, plane_rows) == (5, 5, 5)  # the last job to finish wins, as a whole
    assert all(Path(f).parent.name == "first" for f in files)


def test_description_search_matches_wildcard_characters_literally(client, make_series):
    make_series("chest")
    submit(client, "/api/jobs", json={"path": "chest"})
    for text, matches in (("chest", 1), ("%", 0), ("CH_ST", 0)):
        assert len(client.get("/api/series", params={"description": text}).json()) == matches, text


def test_numbers_beyond_the_database_types_are_rejected_not_crashing(client):
    assert client.get("/api/series/1/axial/3000000000").status_code == 422
    assert client.get(f"/api/series/{2**63}").status_code == 422
    for url in ("/api/jobs", "/api/series"):  # a page cursor beyond bigint
        assert client.get(url, params={"before_id": 2**63}).status_code == 422
        assert client.get(url, params={"before_id": 2**63 - 1}).status_code == 200


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


def test_queue_outage_stops_the_pass_at_the_first_failure(client):
    client.post("/api/jobs", json={"path": "a"})
    client.post("/api/jobs", json={"path": "b"})
    attempts = []

    def unavailable(job_id):
        attempts.append(job_id)
        raise RedisConnectionError("Timeout")

    assert dispatch.publish_due(publish=unavailable) == 0
    assert len(attempts) == 1  # not one socket timeout per due job while Redis hangs
    published = []
    assert dispatch.publish_due(publish=published.append) == 1  # the other job is still due; the failed one waits 5 s


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


def expire(job_id):
    with pool().connection() as conn:
        conn.execute("UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = %s", (job_id,))


def make_due(job_id):
    with pool().connection() as conn:
        conn.execute(
            "UPDATE job_outbox SET next_dispatch_at = now() - interval '1 second' WHERE job_id = %s", (job_id,)
        )


def test_only_one_concurrent_delivery_can_claim_a_job(client):
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
        repository.save_analysis_and_finish_job(claim=stale, series={}, sagittal=b"bad")
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
