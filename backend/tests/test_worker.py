import pytest
from redis import Redis
from rq.job import Job

from services import ingest
from services.worker import IngestJob


@pytest.mark.parametrize("func", ["app.services.ingest.run", ingest.run])
def test_worker_resolves_queued_ingest_job(func):
    connection = Redis()
    queued_job = Job.create(func=func, args=(123,), connection=connection)
    worker_job = IngestJob(connection=connection)
    worker_job.data = queued_job.data

    assert worker_job.func is ingest.run
    assert worker_job.args == (123,)
