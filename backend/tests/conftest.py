from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from app.config import Settings, get_settings
from app.tools.synthetic import write_series


@pytest.fixture(scope="session")
def small_series(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """40 slices of 64x64, 2.5 mm apart, with shuffled (UID) file names."""
    return write_series(
        tmp_path_factory.mktemp("series") / "small",
        slices=40,
        size=64,
        pixel_spacing=5.0,
        slice_spacing=2.5,
        slice_thickness=1.0,
    )


@pytest.fixture
def data_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    for name in ("library", "uploads", "derived"):
        (tmp_path / name).mkdir()
    monkeypatch.setenv("LIBRARY_ROOT", str(tmp_path / "library"))
    monkeypatch.setenv("UPLOAD_ROOT", str(tmp_path / "uploads"))
    monkeypatch.setenv("DERIVED_ROOT", str(tmp_path / "derived"))
    monkeypatch.setenv("WORKER_HEARTBEAT_FILE", str(tmp_path / "heartbeat"))
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


# --- integration tests: need a PostgreSQL server (TEST_DATABASE_URL) ------------------------------


@pytest.fixture
def database_url(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A fresh, migrated database per test, dropped afterwards."""
    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL not set; skipping integration test")
    from app.migrate import migrate

    name = f"test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    url = admin_url.rsplit("/", 1)[0] + f"/{name}"
    migrate(url)
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    try:
        yield url
    finally:
        get_settings.cache_clear()
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
