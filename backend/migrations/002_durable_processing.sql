BEGIN;
ALTER TABLE jobs ADD COLUMN attempt_count integer NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN attempt_token uuid;
ALTER TABLE jobs ADD COLUMN lease_expires_at timestamptz;
ALTER TABLE jobs ADD COLUMN attempt_started_at timestamptz;
ALTER TABLE jobs ADD COLUMN stage text NOT NULL DEFAULT 'queued';
CREATE INDEX jobs_expired_lease_idx ON jobs(lease_expires_at) WHERE status = 'running';
CREATE TABLE job_outbox (
    job_id bigint PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    next_dispatch_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz,
    last_error text
);
CREATE INDEX job_outbox_due_idx ON job_outbox(next_dispatch_at);
-- Existing interrupted jobs can be safely retried after old workers have been stopped.
UPDATE jobs SET status = 'queued', stage = 'queued' WHERE status = 'running';
INSERT INTO job_outbox(job_id) SELECT id FROM jobs WHERE status = 'queued';
COMMIT;
