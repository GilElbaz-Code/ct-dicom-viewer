"""One connection pool per process, opened on first use."""

import os
import threading

from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

from core.config import settings

POOL_SIZE = 10
POOL_WAIT_SECONDS = 5  # a request waiting longer for a connection gets 503 instead of hanging
# No statement may hang a pooled connection: the slowest is a series upsert with its plane, well under a second.
SESSION_OPTIONS = "-c statement_timeout=10000 -c lock_timeout=3000"  # milliseconds


_pool: ConnectionPool[Connection[DictRow]] | None = None
_lock = threading.Lock()  # concurrent first requests must build one pool, not one each


def pool() -> ConnectionPool[Connection[DictRow]]:
    global _pool
    if _pool is None:
        with _lock:
            if _pool is None:
                # dict_row: every query returns plain dicts, which the API turns into Pydantic models.
                _pool = ConnectionPool(
                    conninfo=settings.database_url,
                    connection_class=Connection[DictRow],
                    kwargs={"row_factory": dict_row, "options": SESSION_OPTIONS},
                    check=ConnectionPool.check_connection,  # tested before use, so a database restart is survived
                    min_size=1,
                    max_size=POOL_SIZE,
                    timeout=POOL_WAIT_SECONDS,
                    open=True,
                )
    return _pool


def close_pool() -> None:
    """Close an existing pool without creating an unused one during shutdown."""
    global _pool
    with _lock:
        if _pool is not None:
            _pool.close()
            _pool = None


def _forget_parent_pool() -> None:
    """In a forked child: drop the parent's pool without closing it, and replace a lock the parent may have held."""
    global _pool, _lock
    _pool = None
    _lock = threading.Lock()


# The RQ worker forks a child process per job. A pool created in the parent (its failure callback runs there)
# must not be shared with the child: the child opens its own. psycopg never closes a parent's connection in a child.
if hasattr(os, "register_at_fork"):  # not on Windows, where nothing forks
    os.register_at_fork(after_in_child=_forget_parent_pool)
