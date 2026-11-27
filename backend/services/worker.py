"""RQ support for ingestion jobs already stored in Redis."""

from collections.abc import Callable
from typing import Any

from rq.job import Job

from services import ingest


class IngestJob(Job):
    @property
    def func(self) -> Callable[..., Any] | None:
        if self.func_name == "app.services.ingest.run":
            return ingest.run
        return super().func
