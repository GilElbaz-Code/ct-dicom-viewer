"""Apply SQL migrations in order, once each.

Runs as a one-shot container before the API and worker start. A Postgres
advisory lock makes concurrent runs safe; every file runs in its own
transaction, so a failed migration leaves no half-applied schema.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

import psycopg

from app.config import get_settings

log = logging.getLogger("migrate")
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
LOCK_ID = 727_001  # arbitrary, constant


def _connect(url: str, attempts: int = 30) -> psycopg.Connection:
    for i in range(attempts):
        try:
            return psycopg.connect(url, autocommit=True)
        except psycopg.OperationalError:
            if i == attempts - 1:
                raise
            time.sleep(1)
    raise RuntimeError("unreachable")


def migrate(url: str, directory: Path = MIGRATIONS_DIR) -> list[str]:
    applied_now: list[str] = []
    with _connect(url) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_ID,))
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            done = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
            for path in sorted(directory.glob("*.sql")):
                if path.name in done:
                    continue
                log.info("applying %s", path.name)
                with conn.transaction():
                    conn.execute(path.read_text())
                    conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.name,))
                applied_now.append(path.name)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))
    return applied_now


def main() -> None:
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s %(message)s")
    applied = migrate(get_settings().database_url)
    log.info("migrations applied: %s", ", ".join(applied) if applied else "none (schema up to date)")


if __name__ == "__main__":
    sys.exit(main())
