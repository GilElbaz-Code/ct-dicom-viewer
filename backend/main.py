"""The FastAPI app: routers, error handlers (api/errors.py) and the health check.

Routes are plain `def`: pydicom and psycopg block, so FastAPI runs them in its thread pool.
Run locally with:  uv run uvicorn main:app --reload
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http import HTTPStatus

from fastapi import FastAPI

from api import errors, jobs, series
from db.pool import close_pool


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Release database resources when the API process stops."""
    try:
        yield
    finally:
        close_pool()


app = FastAPI(
    title="CT DICOM Viewer API",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
    exception_handlers=errors.handlers,
)
app.include_router(router=jobs.router)
app.include_router(router=series.router)


@app.get("/api/health", status_code=HTTPStatus.OK, tags=["health"])
def health() -> dict[str, str]:
    """Liveness for the container healthcheck."""
    return {"status": "ok"}
