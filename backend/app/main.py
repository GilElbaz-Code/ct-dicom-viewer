"""FastAPI application.

    uvicorn app.main:app --host 0.0.0.0 --port 8000

Endpoints are plain `def` (not `async def`): DICOM decoding and psycopg calls
block, and FastAPI runs sync endpoints in a thread pool, so they do not stall
the event loop.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import health, jobs, series
from app.config import get_settings
from app.db import create_pool
from app.errors import JobError
from app.logging_setup import configure_logging

log = logging.getLogger("api")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    settings.upload_root.mkdir(parents=True, exist_ok=True)
    app.state.pool = create_pool(settings.database_url, min_size=1, max_size=10, name="ctviewer-api")
    log.info("api started")
    yield
    app.state.pool.close()


def create_app() -> FastAPI:
    app = FastAPI(
        title="CT DICOM Viewer API",
        version="1.0.0",
        description="Index CT DICOM series, track processing jobs and serve viewer images.",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    @app.exception_handler(JobError)
    def job_error_handler(_: Request, exc: JobError) -> JSONResponse:
        return JSONResponse({"detail": exc.message, "code": exc.code}, status_code=400)

    for router in (health.router, jobs.router, series.router):
        app.include_router(router, prefix="/api")
    return app


app = create_app()
