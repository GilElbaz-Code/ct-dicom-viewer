"""Settings, read from environment variables of the same name (DATABASE_URL, REDIS_URL, DATA_DIR)."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql://ctviewer:ctviewer@localhost:5432/ctviewer"
    redis_url: str = "redis://localhost:6379/0"
    data_dir: Path = Path("/data")  # the DICOM library; every submitted path must be inside it
    s3_endpoint: str = "http://storage:9000"
    s3_public_endpoint: str = "http://localhost:9000"
    s3_access_key: str = "ctviewer"
    s3_secret_key: str = "ctviewer-local-storage"
    s3_bucket: str = "ct-studies"
    s3_region: str = "us-east-1"
    upload_origins: list[str] = ["http://localhost:8080", "http://localhost:5173"]
    lease_seconds: int = Field(default=120, ge=10)
    heartbeat_seconds: int = Field(default=20, ge=1)
    job_timeout_seconds: int = Field(default=1800, ge=1)
    max_attempts: int = Field(default=3, ge=1)
    dispatch_interval_seconds: int = Field(default=60, ge=1)
    retry_delay_seconds: int = Field(default=30, ge=1)


settings = Settings()
