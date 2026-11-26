"""Heartbeat a database lease independently of long-running pixel decoding."""

import logging
from threading import Event, Thread
from types import TracebackType

from app.config import settings
from app.db import repository
from app.errors import LeaseLost

logger = logging.getLogger(__name__)


class Lease:
    def __init__(self, *, claim: repository.Claim) -> None:
        self.claim = claim
        self.stopped = Event()
        self.lost = Event()
        self.thread = Thread(target=self._heartbeat, name=f"lease-{claim.job_id}", daemon=True)

    def __enter__(self) -> "Lease":
        self.thread.start()
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.stopped.set()
        self.thread.join(timeout=5)

    def check(self) -> None:
        if self.lost.is_set():
            raise LeaseLost("Lease renewal failed; this attempt has stopped")

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
