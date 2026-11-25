-- Initial schema: DICOM metadata index + job queue.
-- Applied by `python -m app.migrate` (the `migrate` service in docker-compose.yml).

CREATE EXTENSION IF NOT EXISTS pg_trgm;  -- fast ILIKE '%text%' search on descriptions

-- ---------------------------------------------------------------------------
-- Metadata index
-- ---------------------------------------------------------------------------

CREATE TABLE studies (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    study_instance_uid  text NOT NULL UNIQUE,
    patient_id          text,             -- already anonymized in the source data
    study_description   text,
    study_date          date,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX studies_patient_id_idx       ON studies (patient_id);
CREATE INDEX studies_description_idx      ON studies (lower(study_description));
CREATE INDEX studies_description_trgm_idx ON studies USING gin (study_description gin_trgm_ops);

CREATE TABLE series (
    id                      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    series_instance_uid     text NOT NULL UNIQUE,
    study_id                bigint NOT NULL REFERENCES studies (id) ON DELETE CASCADE,
    -- indexed: headers stored, analysis pending | ready: viewer artifacts built
    status                  text NOT NULL DEFAULT 'indexed'
                            CHECK (status IN ('indexed', 'ready', 'analysis_failed')),
    source_path             text NOT NULL,
    modality                text,
    series_description      text,
    series_number           integer,
    rows                    integer NOT NULL,
    columns                 integer NOT NULL,
    num_slices              integer NOT NULL,
    pixel_spacing_row       double precision,  -- mm between rows (PixelSpacing[0])
    pixel_spacing_col       double precision,  -- mm between columns (PixelSpacing[1])
    slice_thickness         double precision,  -- (0018,0050)
    spacing_between_slices  double precision,  -- (0018,0088) as written; often absent
    computed_slice_spacing  double precision,  -- median z step between sorted slices
    z_min                   double precision NOT NULL,
    z_max                   double precision NOT NULL,
    image_orientation       double precision[] NOT NULL,
    attributes              jsonb NOT NULL DEFAULT '{}'::jsonb,  -- long tail of header fields
    warnings                text[] NOT NULL DEFAULT '{}',
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX series_study_id_idx          ON series (study_id);
CREATE INDEX series_slice_thickness_idx   ON series (slice_thickness);
CREATE INDEX series_modality_idx          ON series (modality);
CREATE INDEX series_description_trgm_idx  ON series USING gin (series_description gin_trgm_ops);
CREATE INDEX series_attributes_idx        ON series USING gin (attributes jsonb_path_ops);

-- One row per slice, in volume order. Lets the API open a single file per request.
CREATE TABLE instances (
    series_id         bigint NOT NULL REFERENCES series (id) ON DELETE CASCADE,
    slice_index       integer NOT NULL,          -- 0 = lowest z (most inferior)
    sop_instance_uid  text NOT NULL,
    instance_number   integer,
    z_position        double precision NOT NULL, -- ImagePositionPatient[2]
    file_path         text NOT NULL,
    PRIMARY KEY (series_id, slice_index)
);

CREATE TABLE analysis_results (
    series_id          bigint PRIMARY KEY REFERENCES series (id) ON DELETE CASCADE,
    job_id             bigint,
    sagittal_path      text NOT NULL,
    sagittal_geometry  jsonb NOT NULL,
    qc                 jsonb NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Jobs (PostgreSQL is also the queue: workers claim rows with SKIP LOCKED)
-- ---------------------------------------------------------------------------

CREATE TABLE batches (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    filename      text,
    submitted_by  text NOT NULL,
    total_rows    integer NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE jobs (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind           text NOT NULL CHECK (kind IN ('ingest', 'analyze')),
    status         text NOT NULL DEFAULT 'queued'
                   CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    source_path    text,                                   -- ingest input
    series_id      bigint REFERENCES series (id) ON DELETE SET NULL,
    batch_id       bigint REFERENCES batches (id) ON DELETE CASCADE,
    batch_row      integer,                                -- 1-based CSV data row
    parent_job_id  bigint REFERENCES jobs (id) ON DELETE SET NULL,
    submitted_by   text NOT NULL,
    attempts       integer NOT NULL DEFAULT 0,
    max_attempts   integer NOT NULL DEFAULT 3,
    run_after      timestamptz NOT NULL DEFAULT now(),     -- retry backoff
    locked_until   timestamptz,                            -- lease; expired = worker died
    worker_id      text,
    error_code     text,
    error_message  text,
    result         jsonb,
    created_at     timestamptz NOT NULL DEFAULT now(),
    started_at     timestamptz,
    finished_at    timestamptz,
    updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX jobs_queued_idx        ON jobs (kind, id) WHERE status = 'queued';
CREATE INDEX jobs_running_lease_idx ON jobs (locked_until) WHERE status = 'running';
CREATE INDEX jobs_batch_idx         ON jobs (batch_id, batch_row);
CREATE INDEX jobs_series_idx        ON jobs (series_id);
CREATE INDEX jobs_created_idx       ON jobs (created_at DESC);
CREATE INDEX jobs_submitted_by_idx  ON jobs (submitted_by, created_at DESC);

-- At most one active analysis per series, so retries and re-ingests cannot pile up duplicates.
CREATE UNIQUE INDEX jobs_one_active_per_series_idx
    ON jobs (kind, series_id) WHERE status IN ('queued', 'running');
