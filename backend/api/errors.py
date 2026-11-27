"""The mapping from service errors to HTTP status codes, passed to FastAPI as `exception_handlers`."""

from http import HTTPStatus

from fastapi import Request
from fastapi.responses import JSONResponse
from psycopg_pool import PoolTimeout

from core.errors import Conflict, InvalidInput, NotFound


def not_found(_: Request, exc: NotFound) -> JSONResponse:
    return JSONResponse(content={"detail": str(exc)}, status_code=HTTPStatus.NOT_FOUND)


def conflict(_: Request, exc: Conflict) -> JSONResponse:
    # no-store: once the client has the new version, a cached 409 must not answer it again.
    return JSONResponse(
        content={"detail": str(exc)}, status_code=HTTPStatus.CONFLICT, headers={"Cache-Control": "no-store"}
    )


def invalid_input(_: Request, exc: InvalidInput) -> JSONResponse:
    return JSONResponse(content={"detail": str(exc)}, status_code=HTTPStatus.UNPROCESSABLE_CONTENT)


def overloaded(_: Request, exc: PoolTimeout) -> JSONResponse:
    """Every database connection stayed busy: tell the client to retry instead of hanging the request."""
    return JSONResponse(
        content={"detail": "The server is busy; try again shortly"},
        status_code=HTTPStatus.SERVICE_UNAVAILABLE,
        headers={"Retry-After": "2"},
    )


handlers = {NotFound: not_found, Conflict: conflict, InvalidInput: invalid_input, PoolTimeout: overloaded}
