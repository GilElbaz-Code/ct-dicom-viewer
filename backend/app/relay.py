"""Run with python -m app.relay; PostgreSQL owns retries and recovery."""

import logging
import signal
from threading import Event
from types import FrameType

from app.db import dispatch
from app.db.pool import close_pool
from app.services import jobs, uploads

logger = logging.getLogger(__name__)


def tick() -> None:
    dispatch.recover_expired()
    dispatch.publish_due(publish=jobs.publish)
    uploads.cleanup_expired()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    stop = Event()

    def shutdown(signum: int, frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        while not stop.is_set():
            try:
                tick()
            except Exception:
                logger.exception("Relay pass failed; retrying")
            stop.wait(timeout=2)
    finally:
        close_pool()


if __name__ == "__main__":
    main()
