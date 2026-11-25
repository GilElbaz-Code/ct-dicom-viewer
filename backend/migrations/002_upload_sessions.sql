-- Completion and its ingest job commit together; retries return the same job.
CREATE TABLE upload_sessions (
    id uuid PRIMARY KEY,
    job_id bigint REFERENCES jobs (id),
    created_at timestamptz NOT NULL DEFAULT now()
);
