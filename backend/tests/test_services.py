import pytest

from app.config import settings
from app.errors import InvalidInput
from app.services import ingest, jobs, viewer

POSITIONS = [-10.0, -7.5, -5.0, -2.5, 0.0]


@pytest.mark.parametrize(("z", "index"), [(-10, 0), (-6.0, 2), (-6.5, 1), (100, 4), (-100, 0)])
def test_locate_finds_the_closest_slice(z, index):
    assert viewer.locate(POSITIONS, 2.5, z, None)["index"] == index


def test_locate_flags_locations_outside_the_scanned_range():
    assert viewer.locate(POSITIONS, 2.5, 1.0, None)["outside"] is False  # within half a slice of the top
    assert viewer.locate(POSITIONS, 2.5, 1.5, None)["outside"] is True


def test_locate_by_index():
    assert viewer.locate(POSITIONS, 2.5, None, 3) == {
        "index": 3,
        "z": -2.5,
        "num_slices": 5,
        "distance_mm": 0.0,
        "outside": False,
    }


@pytest.mark.parametrize(("z", "index"), [(None, 5), (None, -1), (1.0, 1), (None, None)])
def test_locate_rejects_bad_requests(z, index):
    with pytest.raises(InvalidInput):
        viewer.locate(POSITIONS, 2.5, z, index)


def test_parse_csv_takes_the_first_column_and_skips_header_and_blank_rows():
    assert jobs.parse_csv("﻿path\nseries_1\n\n  series_2 \n".encode()) == ["series_1", "series_2"]
    assert jobs.parse_csv(b"a/b,comment\nc\n") == ["a/b", "c"]


@pytest.mark.parametrize("content", [b"path\n", b"", "caf\xe9".encode("latin-1")])
def test_parse_csv_rejects_empty_or_non_utf8_files(content):
    with pytest.raises(InvalidInput):
        jobs.parse_csv(content)


def test_parse_csv_rejects_malformed_quotes():
    with pytest.raises(InvalidInput, match="Invalid CSV"):
        jobs.parse_csv(content=b'path\n"unclosed')


def test_paths_must_stay_inside_the_data_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    (tmp_path / "series_1").mkdir()
    assert ingest.resolve_inside_data_dir("series_1") == (tmp_path / "series_1").resolve()
    with pytest.raises(InvalidInput, match="outside"):
        ingest.resolve_inside_data_dir("../")
    with pytest.raises(InvalidInput, match="not found"):
        ingest.resolve_inside_data_dir("missing")
    for whole_library in (".", "  ", "series_1/.."):
        with pytest.raises(InvalidInput, match="not the data directory itself"):
            ingest.resolve_inside_data_dir(whole_library)


class FakeQueue:
    """Records enqueues; fetch_job reports the status RQ would."""

    def __init__(self):
        self.status: dict[str, str] = {}
        self.enqueued: list[str] = []

    def fetch_job(self, job_id):
        status = self.status.get(job_id)
        return None if status is None else type("Job", (), {"get_status": lambda _: status})()

    def enqueue(self, func, *args, job_id, **kwargs):
        self.enqueued.append(job_id)
        self.status[job_id] = "queued"


def test_publish_does_not_stack_a_job_that_is_still_waiting_in_the_queue(monkeypatch):
    fake = FakeQueue()
    monkeypatch.setattr(jobs, "queue", lambda: fake)
    jobs.publish(job_id=7)
    jobs.publish(job_id=7)  # the relay's 60-second republish while the job waits behind others
    assert fake.enqueued == ["ingest-7"]
    fake.status["ingest-7"] = "failed"  # e.g. its process was killed; the database lease decides what happens next
    jobs.publish(job_id=7)
    assert fake.enqueued == ["ingest-7", "ingest-7"]
    del fake.status["ingest-7"]  # lost from Redis entirely
    jobs.publish(job_id=7)
    assert len(fake.enqueued) == 3
