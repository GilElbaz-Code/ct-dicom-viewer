-- Metadata index and job history.
-- Postgres runs this file once, when the database volume is first created
-- (docker-compose.yml mounts it into /docker-entrypoint-initdb.d).

CREATE TABLE series (
    id                      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    series_uid              text NOT NULL UNIQUE,   -- SeriesInstanceUID: re-indexing updates the same row
    study_uid               text NOT NULL,          -- StudyInstanceUID: several series can share a study
    patient_id              text,                   -- already anonymized in the source data
    study_description       text,
    series_description      text,
    modality                text,
    pixel_spacing           float8[],               -- [row, column] spacing in mm
    slice_thickness         float8,
    spacing_between_slices  float8,                 -- header tag as written: often missing, sometimes wrong
    slice_spacing           float8 NOT NULL,        -- measured: median distance between slice positions
    rows                    int NOT NULL,
    columns                 int NOT NULL,
    z_positions             float8[] NOT NULL,      -- ImagePositionPatient z of every slice, ascending
    files                   text[] NOT NULL,        -- slice files, in the same order as z_positions
    sagittal                bytea,                  -- analysis output: mid-volume sagittal plane, int16 HU
    source_path             text NOT NULL,
    updated_at              timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX series_slice_thickness_idx ON series (slice_thickness);
CREATE INDEX series_study_uid_idx ON series (study_uid);

CREATE TABLE jobs (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    path                text NOT NULL,              -- DICOM directory as the user submitted it
    batch_id            uuid,                       -- shared by all jobs created from one CSV
    status              text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'done', 'failed')),
    stage               text NOT NULL DEFAULT 'queued',  -- progress shown to the user: headers, analysis, complete
    error               text,                       -- why it failed, shown to the user
    series_id           bigint REFERENCES series (id) ON DELETE SET NULL,
    attempt_count       int NOT NULL DEFAULT 0,     -- attempts started; capped by MAX_ATTEMPTS
    attempt_token       uuid,                       -- fencing token: only the current attempt may write
    attempt_started_at  timestamptz,                -- the hard deadline counts from here
    lease_expires_at    timestamptz,                -- renewed by the worker's heartbeat; expiry means recovery
    created_at          timestamptz NOT NULL DEFAULT now(),
    finished_at         timestamptz
);

CREATE INDEX jobs_batch_id_idx ON jobs (batch_id);
CREATE INDEX jobs_expired_lease_idx ON jobs (lease_expires_at) WHERE status = 'running';

-- Transactional outbox: a job and its dispatch commit together; the relay publishes due rows to Redis.
CREATE TABLE job_outbox (
    job_id            bigint PRIMARY KEY REFERENCES jobs (id) ON DELETE CASCADE,
    next_dispatch_at  timestamptz NOT NULL DEFAULT now(),  -- publish (again) at or after this time
    published_at      timestamptz,                          -- NULL while waiting for a first publish or a retry
    last_error        text                                  -- why the last publish failed (e.g. Redis down)
);

CREATE INDEX job_outbox_due_idx ON job_outbox (next_dispatch_at);
