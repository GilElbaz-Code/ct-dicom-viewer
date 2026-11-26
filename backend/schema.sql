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
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    path         text NOT NULL,                     -- DICOM directory as the user submitted it
    batch_id     uuid,                              -- shared by all jobs created from one CSV
    status       text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'done', 'failed')),
    error        text,                              -- why it failed, shown to the user
    series_id    bigint REFERENCES series (id) ON DELETE SET NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz
);

CREATE INDEX jobs_batch_id_idx ON jobs (batch_id);

-- Upload sessions and durable dispatch.
CREATE TABLE upload_sessions (
    id uuid PRIMARY KEY,
    name text NOT NULL,
    manifest jsonb NOT NULL,
    status text NOT NULL DEFAULT 'uploading' CHECK (status IN ('uploading', 'complete', 'expired')),
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL DEFAULT now() + interval '24 hours',
    cleaned_at timestamptz,
    job_id bigint UNIQUE REFERENCES jobs(id)
);
ALTER TABLE jobs ADD COLUMN upload_id uuid REFERENCES upload_sessions(id);
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
