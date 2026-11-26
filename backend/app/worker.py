"""RQ supervises job processes; PostgreSQL leases handle interruption and retries."""

from rq import Worker as RQWorker


class Worker(RQWorker):
    """Keep the deployment entry point; expired database leases are recovered by the relay."""
