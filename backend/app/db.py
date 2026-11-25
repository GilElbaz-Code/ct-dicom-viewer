"""PostgreSQL connection pool (psycopg 3).

`with pool.connection() as conn:` is one transaction: it commits when the
block exits normally and rolls back if it raises.
"""

from __future__ import annotations

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


def create_pool(database_url: str, *, min_size: int = 1, max_size: int = 10, name: str = "ctviewer") -> ConnectionPool:
    pool = ConnectionPool(
        database_url,
        min_size=min_size,
        max_size=max_size,
        kwargs={"row_factory": dict_row, "application_name": name},
        open=False,
    )
    pool.open(wait=True, timeout=30)
    return pool
