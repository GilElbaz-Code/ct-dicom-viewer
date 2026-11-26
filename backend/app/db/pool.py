"""One connection pool per process, opened on first use."""

import os
from functools import cache

from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

from app.config import settings


@cache
def pool() -> ConnectionPool[Connection[DictRow]]:
    # dict_row: every query returns plain dicts, which the API turns into Pydantic models.
    return ConnectionPool(
        conninfo=settings.database_url,
        connection_class=Connection[DictRow],
        kwargs={"row_factory": dict_row},
        check=ConnectionPool.check_connection,  # test each connection before use, so a database restart is survived
        min_size=1,
        max_size=10,
        open=True,
    )


def close_pool() -> None:
    """Close an existing pool without creating an unused one during shutdown."""
    if pool.cache_info().currsize:
        pool().close()
        pool.cache_clear()


# The RQ worker forks a child process per job. A pool created in the parent (its failure callback runs there)
# must not be shared with the child: the child opens its own. psycopg never closes a parent's connection in a child.
if hasattr(os, "register_at_fork"):  # not on Windows, where nothing forks
    os.register_at_fork(after_in_child=pool.cache_clear)
