import threading
import time

import pytest

from app.db import pool as pool_module
from app.errors import InvalidInput
from app.services import jobs, viewer

POSITIONS = [-10.0, -7.5, -5.0, -2.5, 0.0]


@pytest.mark.parametrize(
    ("z", "index"),
    [
        (-10, 0),  # exact hits, both ends and inside
        (-5.0, 2),
        (0.0, 4),
        (-6.0, 2),
        (-6.5, 1),
        (-8.75, 0),  # ties pick the lower index
        (-1.25, 3),
        (100, 4),  # beyond an end: that end slice
        (-100, 0),
    ],
)
def test_locate_finds_the_closest_slice(z, index):
    assert viewer.locate(POSITIONS, 2.5, z, None)["index"] == index


def test_locate_by_index():
    assert viewer.locate(POSITIONS, 2.5, None, 3) == {
        "index": 3,
        "z": -2.5,
        "num_slices": 5,
        "distance_mm": 0.0,
        "outside": False,
    }


@pytest.mark.parametrize(
    ("z", "index"),
    [(None, 5), (None, -1), (1.0, 1), (None, None), (float("nan"), None), (float("inf"), None), (float("-inf"), None)],
)
def test_locate_rejects_bad_requests(z, index):
    with pytest.raises(InvalidInput):
        viewer.locate(POSITIONS, 2.5, z, index)


def test_parse_csv_takes_the_first_column_and_skips_header_and_blank_rows():
    assert jobs.parse_csv("﻿path\nseries_1\n\n  series_2 \n".encode()) == ["series_1", "series_2"]
    assert jobs.parse_csv(b"a/b,comment\nc\n") == ["a/b", "c"]
    rows = [f"s{i}" for i in range(1000)]  # the limit counts data rows: not the header, not blank rows
    assert jobs.parse_csv(("path\n\n" + "\n\n".join(rows) + "\n").encode()) == rows
    with pytest.raises(InvalidInput, match="more than 1,000"):
        jobs.parse_csv(("path\n" + "\n".join([*rows, "s1000"]) + "\n").encode())


def test_concurrent_first_requests_share_one_pool(monkeypatch):
    built = []
    inside = threading.Event()
    release = threading.Event()

    class SlowPool:
        check_connection = None

        def __init__(self, **kwargs):
            built.append(self)
            inside.set()
            release.wait(timeout=5)  # hold the first construction while the other threads arrive

    monkeypatch.setattr(pool_module, "ConnectionPool", SlowPool)
    monkeypatch.setattr(pool_module, "_pool", None)
    start = threading.Barrier(8)
    results = []

    def first_request():
        start.wait()
        results.append(pool_module.pool())

    threads = [threading.Thread(target=first_request) for _ in range(8)]
    for thread in threads:
        thread.start()
    assert inside.wait(timeout=5)
    time.sleep(0.05)  # every thread is past the barrier, waiting for the one constructing
    release.set()
    for thread in threads:
        thread.join(timeout=5)
    assert len(built) == 1
    assert len(results) == 8
    assert all(result is built[0] for result in results)
