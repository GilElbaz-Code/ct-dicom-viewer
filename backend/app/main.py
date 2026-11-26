"""The FastAPI app: routers plus the mapping from service errors to HTTP status codes.

Routes are plain `def`: pydicom and psycopg block, so FastAPI runs them in its thread pool.
Run locally with:  uv run uvicorn app.main:app --reload
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import jobs, series
from app.db.pool import close_pool
from app.errors import InvalidInput, NotFound


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Release database resources when the API process stops."""
    try:
        yield
    finally:
        close_pool()


app = FastAPI(title="CT DICOM Viewer API", docs_url="/api/docs", openapi_url="/api/openapi.json", lifespan=lifespan)
app.include_router(router=jobs.router)
app.include_router(router=series.router)


@app.exception_handler(NotFound)
def not_found(_: Request, exc: NotFound) -> JSONResponse:
    return JSONResponse(content={"detail": str(exc)}, status_code=404)


@app.exception_handler(InvalidInput)
def invalid_input(_: Request, exc: InvalidInput) -> JSONResponse:
    return JSONResponse(content={"detail": str(exc)}, status_code=422)


@app.get("/api/health", tags=["health"])
def health() -> dict[str, str]:
    """Liveness for the container healthcheck."""
    return {"status": "ok"}
