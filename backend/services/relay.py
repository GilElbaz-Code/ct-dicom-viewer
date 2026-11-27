"""Run with python -m services.relay to publish jobs and recover expired leases."""

import signal
from threading import Event
from types import FrameType

from core.logger import logger
from db import dispatch
from db.pool import close_pool
from services import jobs

POLL_SECONDS = 2  # the pause between passes when there is no backlog


def tick() -> bool:
    """One pass. True when a batch was full, so more work is waiting and the relay goes again at once."""
    recovered = dispatch.recover_expired()
    published = dispatch.publish_due(publish=jobs.publish)
    return dispatch.BATCH in (recovered, published)


def main() -> None:
    stop = Event()

    def shutdown(signum: int, frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        while not stop.is_set():
            try:
                if tick():
                    continue
            except Exception:
                logger.exception("Relay pass failed; retrying")
            stop.wait(timeout=POLL_SECONDS)
    finally:
        close_pool()


if __name__ == "__main__":
    main()
