"""End to end through the HTTP API against a real PostgreSQL.

Skipped unless TEST_DATABASE_URL is set: the test drops and recreates the tables in that database.
No Redis or worker: `submit` publishes the due outbox rows and runs each job in this process.
"""

import io
import os
from pathlib import Path
from uuid import uuid4

import numpy as np
import psycopg
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from core.config import settings
from core.errors import LeaseLost
from db import dispatch, repository
from db.pool import close_pool, pool
from main import app
from services import dicom, ingest
from tests.conftest import write_slice

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="set TEST_DATABASE_URL to a disposable PostgreSQL database")


def submit(client, path, **kwargs):
    response = client.post(path, **kwargs)
    pending = []
    dispatch.publish_due(publish=pending.extend)
    for job_id in pending:
        ingest.run(job_id=job_id)
    return response


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_url", DATABASE_URL)
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    close_pool()
    with pool().connection() as conn:
        conn.execute("DROP TABLE IF EXISTS job_outbox, jobs, series CASCADE")
        conn.execute((Path(__file__).parents[1] / "schema.sql").read_text())
    yield TestClient(app)
    close_pool()


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


def expire(job_id):
    with pool().connection() as conn:
        conn.execute("UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = %s", (job_id,))


def make_due(job_id):
    with pool().connection() as conn:
        conn.execute(
            "UPDATE job_outbox SET next_dispatch_at = now() - interval '1 second' WHERE job_id = %s", (job_id,)
        )


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
        repository.save_analysis_and_finish_job(claim=stale, series={}, sagittal=b"bad", encoding="float32le")
    assert repository.renew_lease(claim=current) is True
    assert client.get("/api/jobs").json()[0]["status"] == "running"


def test_requests_for_a_replaced_version_get_409_instead_of_mixing_versions(client, make_series, tmp_path):
    make_series("chest", n=5)
    submit(client, "/api/jobs", json={"path": "chest"})
    [series] = client.get("/api/series").json()
    series_id, old = series["id"], series["updated_at"]
    urls = [
        (f"/api/series/{series_id}/locate", {"z": -95}),
        (f"/api/series/{series_id}/axial/2", {}),
        (f"/api/series/{series_id}/sagittal", {}),
    ]
    for url, params in urls:
        assert client.get(url, params={**params, "v": old}).status_code == 200

    # After the viewer read the metadata, a sixth slice arrives and the folder is indexed again.
    write_slice(tmp_path / "chest" / "000.dcm", z=-87.5, value=50)
    submit(client, "/api/jobs", json={"path": "chest"})
    current = client.get(f"/api/series/{series_id}").json()
    new = current["updated_at"]
    assert current["num_slices"] == 6
    assert new != old
    for url, params in urls:
        stale = client.get(url, params={**params, "v": old})
        assert stale.status_code == 409
        assert stale.headers["cache-control"] == "no-store"
        assert "indexed again" in stale.json()["detail"]
        assert client.get(url, params={**params, "v": new}).status_code == 200
        assert client.get(url, params=params).status_code == 200  # without v: the current version
    assert client.get("/api/series/999999/axial/0", params={"v": old}).status_code == 404
    assert client.get("/api/series/999999/locate", params={"z": 0, "v": old}).status_code == 404


def test_fractional_hu_survive_storage_and_match_the_axial_window(client, tmp_path):
    directory = tmp_path / "fractional"
    directory.mkdir()
    for z in range(3):  # stored 160 * 0.25 + 0.75 = 40.75 HU
        write_slice(directory / f"{z}.dcm", z=z, value=160, rescale=(0.25, 0.75))
    submit(client, "/api/jobs", json={"path": "fractional"})
    series_id = client.get("/api/jobs").json()[0]["series_id"]
    with pool().connection() as conn:
        row = conn.execute("SELECT sagittal, sagittal_encoding FROM series WHERE id = %s", (series_id,)).fetchone()
    assert row["sagittal_encoding"] == "float32le"
    assert set(np.frombuffer(row["sagittal"], dtype="<f4").tolist()) == {40.75}
    window = {"window": 1, "level": 40.5}  # 40 HU would be black; 40.75 HU is grey 191
    for url in (f"/api/series/{series_id}/axial/1", f"/api/series/{series_id}/sagittal"):
        pixels = np.array(Image.open(io.BytesIO(client.get(url, params=window).content)))
        assert set(pixels.ravel().tolist()) == {191}


def test_the_migration_keeps_int16_planes_of_an_older_database_readable(client, make_series):
    make_series("chest", n=3)
    submit(client, "/api/jobs", json={"path": "chest"})
    [series] = client.get("/api/series").json()
    url = f"/api/series/{series['id']}/sagittal"
    expected = client.get(url).content
    with pool().connection() as conn:  # turn the row into what an older version wrote: int16 HU, no encoding column
        plane = conn.execute("SELECT sagittal FROM series").fetchone()["sagittal"]
        old_bytes = np.frombuffer(plane, dtype="<f4").astype("<i2").tobytes()
        conn.execute("ALTER TABLE series DROP COLUMN sagittal_encoding")
        conn.execute("UPDATE series SET sagittal = %s", (old_bytes,))
    migration = (Path(__file__).parents[1] / "migrations" / "001_sagittal_encoding.sql").read_text()
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:  # the script has its own BEGIN and COMMIT
        conn.execute(migration)
        conn.execute(migration)  # running it twice changes nothing
    with pool().connection() as conn:
        row = conn.execute("SELECT sagittal, sagittal_encoding FROM series").fetchone()
    assert (bytes(row["sagittal"]), row["sagittal_encoding"]) == (old_bytes, "int16le")
    assert client.get(url).content == expected  # the synthetic HU are whole numbers, so nothing was lost

    with pytest.raises(psycopg.errors.CheckViolation), pool().connection() as conn:  # rolled back, then caught
        conn.execute("UPDATE series SET sagittal_encoding = NULL")  # a plane without its format is refused
    submit(client, "/api/jobs", json={"path": "chest"})  # indexing again writes float32
    with pool().connection() as conn:
        assert conn.execute("SELECT sagittal_encoding FROM series").fetchone()["sagittal_encoding"] == "float32le"
