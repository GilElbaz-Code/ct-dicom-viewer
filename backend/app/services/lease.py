"""Heartbeat a database lease independently of long-running pixel decoding."""

import logging
import time
from threading import Event, Thread
from types import TracebackType

from app.config import settings
from app.db import repository
from app.errors import InvalidInput, LeaseLost

logger = logging.getLogger(__name__)

HEARTBEAT_JOIN_SECONDS = 5  # how long leaving the lease waits for a renewal in flight
DEADLINE_SHARE = 0.95  # stop at 95% of JOB_TIMEOUT_SECONDS, so the timeout is still recorded under a valid lease


class Lease:
    def __init__(self, *, claim: repository.Claim) -> None:
        self.claim = claim
        self.stopped = Event()
        self.lost = Event()
        self.deadline = time.monotonic() + settings.job_timeout_seconds * DEADLINE_SHARE
        self.thread = Thread(target=self._heartbeat, name=f"lease-{claim.job_id}", daemon=True)

    def __enter__(self) -> "Lease":
        self.thread.start()
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.stopped.set()
        self.thread.join(timeout=HEARTBEAT_JOIN_SECONDS)

    def check(self) -> None:
        if self.lost.is_set():
            raise LeaseLost("Lease renewal failed; this attempt has stopped")
        if time.monotonic() > self.deadline:  # a deterministic cost, so not retried: another attempt would time out too
            raise InvalidInput(f"Processing took longer than the {settings.job_timeout_seconds} s limit")

    def _heartbeat(self) -> None:
        interval = min(settings.heartbeat_seconds, settings.lease_seconds / 3)
        while not self.stopped.wait(timeout=interval):
            try:
                if repository.renew_lease(claim=self.claim):
                    continue
            except Exception:
                logger.exception("Lease renewal failed for job %s", self.claim.job_id)
            self.lost.set()
            return
