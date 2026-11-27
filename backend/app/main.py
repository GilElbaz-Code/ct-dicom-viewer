"""The FastAPI app: routers plus the mapping from service errors to HTTP status codes.

Routes are plain `def`: pydicom and psycopg block, so FastAPI runs them in its thread pool.
Run locally with:  uv run uvicorn app.main:app --reload
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from psycopg_pool import PoolTimeout

from app.api import jobs, series
from app.db.pool import close_pool
from app.errors import Conflict, InvalidInput, NotFound


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
    return JSONResponse(content={"detail": str(exc)}, status_code=HTTPStatus.NOT_FOUND)


@app.exception_handler(Conflict)
def conflict(_: Request, exc: Conflict) -> JSONResponse:
    # no-store: once the client has the new version, a cached 409 must not answer it again.
    return JSONResponse(
        content={"detail": str(exc)}, status_code=HTTPStatus.CONFLICT, headers={"Cache-Control": "no-store"}
    )


@app.exception_handler(InvalidInput)
def invalid_input(_: Request, exc: InvalidInput) -> JSONResponse:
    return JSONResponse(content={"detail": str(exc)}, status_code=HTTPStatus.UNPROCESSABLE_CONTENT)


@app.exception_handler(PoolTimeout)
def overloaded(_: Request, exc: PoolTimeout) -> JSONResponse:
    """Every database connection stayed busy: tell the client to retry instead of hanging the request."""
    return JSONResponse(
        content={"detail": "The server is busy; try again shortly"},
        status_code=HTTPStatus.SERVICE_UNAVAILABLE,
        headers={"Retry-After": "2"},
    )


@app.get("/api/health", status_code=HTTPStatus.OK, tags=["health"])
def health() -> dict[str, str]:
    """Liveness for the container healthcheck."""
    return {"status": "ok"}
