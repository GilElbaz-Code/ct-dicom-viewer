from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Header, Request
from psycopg import Connection

from app.config import Settings, get_settings


def get_conn(request: Request) -> Iterator[Connection]:
    """One pooled connection per request. Write endpoints call conn.commit() before returning,
    so a client that polls right after a POST always sees the new row."""
    with request.app.state.pool.connection() as conn:
        yield conn


def current_user(x_forwarded_user: Annotated[str | None, Header()] = None) -> str:
    """Identity set by the authenticating reverse proxy (SSO). Without one, everyone is 'anonymous'."""
    return (x_forwarded_user or "anonymous").strip()[:200] or "anonymous"


Conn = Annotated[Connection, Depends(get_conn)]
AppSettings = Annotated[Settings, Depends(get_settings)]
User = Annotated[str, Depends(current_user)]
