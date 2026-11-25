"""Runtime settings, read once from environment variables (12-factor style)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_url: str
    library_root: Path  # read-only DICOM library mounted from the host
    upload_root: Path  # series uploaded through the web app
    derived_root: Path  # artifacts written by the analysis worker
    job_lease_seconds: int
    job_max_attempts: int
    worker_poll_seconds: float
    worker_kinds: tuple[str, ...]
    worker_heartbeat_file: Path
    max_upload_file_mb: int
    max_batch_rows: int
    log_level: str

    @property
    def source_roots(self) -> tuple[Path, ...]:
        """Directories that ingest jobs are allowed to read from."""
        return (self.library_root, self.upload_root)


def _database_url() -> str:
    if url := os.environ.get("DATABASE_URL"):
        return url
    password = os.environ.get("POSTGRES_PASSWORD", "")
    password_file = os.environ.get("POSTGRES_PASSWORD_FILE")
    if password_file:  # Docker/Kubernetes secret mounted as a file
        password = Path(password_file).read_text().strip()
    user = os.environ.get("POSTGRES_USER", "ctviewer")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "ctviewer")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


@lru_cache
def get_settings() -> Settings:
    env = os.environ.get
    return Settings(
        database_url=_database_url(),
        library_root=Path(env("LIBRARY_ROOT", "/data/library")),
        upload_root=Path(env("UPLOAD_ROOT", "/data/uploads")),
        derived_root=Path(env("DERIVED_ROOT", "/data/derived")),
        job_lease_seconds=int(env("JOB_LEASE_SECONDS", "120")),
        job_max_attempts=int(env("JOB_MAX_ATTEMPTS", "3")),
        worker_poll_seconds=float(env("WORKER_POLL_SECONDS", "1.0")),
        worker_kinds=tuple(k.strip() for k in env("WORKER_KINDS", "ingest,analyze").split(",") if k.strip()),
        worker_heartbeat_file=Path(env("WORKER_HEARTBEAT_FILE", "/tmp/worker-heartbeat")),
        max_upload_file_mb=int(env("MAX_UPLOAD_FILE_MB", "50")),
        max_batch_rows=int(env("MAX_BATCH_ROWS", "5000")),
        log_level=env("LOG_LEVEL", "INFO"),
    )
