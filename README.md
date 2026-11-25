# CT DICOM Viewer

Home assignment for the Python Backend Engineer position, Samueli Integrative Cancer Pioneering Institute.

A small CT processing service: index DICOM series (by choosing a directory, uploading a folder or submitting a
CSV batch), track the jobs, query the metadata across studies, and view any series as an axial slice at a
chosen location plus a sagittal view with that location marked.

| Part | Where |
|---|---|
| 1. System design | [`docs/design.md`](docs/design.md) |
| 2. Implementation | `backend/` (FastAPI, worker, migrations), `frontend/` (React + TypeScript), `docker-compose.yml` |
| 3. Docker and DevOps | [`docs/devops.md`](docs/devops.md) |

![Viewer: axial slice at z = 98.75 mm, sagittal view with the location marked, series metadata](docs/images/viewer.png)

<details>
<summary>Import &amp; jobs: batch rows, failures with their reason, retry</summary>

![Import and jobs](docs/images/import.png)

</details>

*Screenshots use the synthetic phantom from `demo-data` (the sphere on the midline appears round in the
sagittal view only if the aspect ratio is right).*

## Run it from scratch

Requirements: Docker with Compose v2 (Docker Desktop, or Docker Engine 24+). Nothing else.

```bash
git clone <this repository> ct-dicom-viewer
cd ct-dicom-viewer
```

**1. Put the DICOM data in `data/`.** Download the two sample series from the shared Google Drive folder and
extract them so each series is its own folder:

```
data/
├── 0x23D38C868F3ED85E20/   *.dcm
└── 0xE13CE7B52F313D3C82/   *.dcm
```

No sample data at hand? Generate a synthetic CT phantom instead (or as well):

```bash
docker compose --profile tools run --rm demo-data      # writes data/synthetic_chest/
```

**2. Start the stack.**

```bash
docker compose up --build -d
```

The first build takes a few minutes. Then open **http://localhost:8080**. (Port taken? `WEB_PORT=8090 docker compose up -d`.)

**3. Index the series.** In **Import & jobs**, click **Index** next to each directory, or submit
[`examples/sample_batch.csv`](examples/sample_batch.csv) under *Batch from CSV*. Each series is indexed in
seconds and analyzed shortly after; the jobs table updates by itself.

**4. View.** In **Viewer**, pick a series, enter a location in mm (or switch to slice index) and press
**Show**. Change the window/level with the presets or the width/level fields (default soft tissue 400/40).

To check everything at once from the command line (needs `curl` and `python3`): `scripts/smoke_test.sh synthetic_chest` (after the
demo-data step). Stop with `docker compose down`; add `-v` to also delete the database and derived data.

## What the app does

* **Viewer**: the axial slice closest to the requested z (`ImagePositionPatient[2]`, in mm) or at a slice index;
  a sagittal image through the middle of the volume, in true physical proportions, with a line at the chosen
  location; the series metadata table; window/level presets and manual values; a slider to scroll slices.
* **Series**: queries across all indexed studies, e.g. slice thickness under 1.5 mm, or a study description.
* **Import & jobs**: index a server directory, upload a folder from your computer, or submit a CSV; every job
  (and every CSV row) shows its status, attempts, duration and, when it fails, the reason, with a Retry button.
  Jobs are stored in the database, so you can close the browser and come back later.

Some behaviour worth knowing:

* Slice index 0 is the **most inferior** slice (lowest z); slices are always sorted by position, never by
  file name or instance number.
* A location outside the scanned range shows the nearest slice and says it is outside.
* *Spacing between slices* shows the DICOM tag when present; otherwise the median distance between slice
  positions, marked *computed* (many CT series do not write the tag).
* Directories with several series, no DICOM images, duplicate slice positions or non-axial images fail with a
  specific reason. Gaps between slices produce a warning and are interpolated in the sagittal view.

### CSV format

One DICOM directory per row. Paths are relative to `data/` (or absolute inside the container, e.g.
`/data/library/…`). A header row is optional; with several columns, name the path column `path`.

```csv
path
0x23D38C868F3ED85E20
0xE13CE7B52F313D3C82
```

[`examples/batch_with_errors.csv`](examples/batch_with_errors.csv) shows how bad rows are reported.

## Why PostgreSQL

The metadata is written by several processes at once (the API and every worker) and queried by researchers at
the same time. PostgreSQL handles concurrent writers natively, while SQLite serializes all writes and DuckDB
allows a single writing process. PostgreSQL also serves as the **job queue** (`FOR UPDATE SKIP LOCKED`), so a
job's result and its status commit in one transaction and the stack needs no Redis or RabbitMQ. JSONB with a
GIN index keeps the long tail of DICOM header fields queryable without migrations, and `pg_trgm` makes
"description contains" searches fast. It runs as a container here and as a managed service in production.
DuckDB would be my choice for large analytical exports, not as the system of record.

**At a million series:** the `series` table itself (about a million rows) is small for PostgreSQL; the pressure
is elsewhere.

* `instances` (one row per slice) would reach ~500 million rows. I would move the per-slice list out of the
  database into a per-series manifest file next to the derived data (it is only ever read whole), or hash-partition
  the table by `series_id`.
* The search page: keyset pagination instead of `OFFSET`, estimated rather than exact counts, and composite or
  partial indexes chosen from the real query log.
* `jobs` grows forever: partition it by month and archive finished jobs, keeping the hot queue small. At high
  job rates, move the queue to a broker (RabbitMQ/SQS) and keep job history in Postgres.
* Read replicas for researcher queries, PgBouncer in front as API and worker replicas multiply.
* Population-level analytics over all headers: a nightly export to Parquet, queried with DuckDB.

## Architecture in one paragraph

nginx serves the React app and proxies `/api` to FastAPI. Every submission (directory, upload, CSV row) creates an
`ingest` job in PostgreSQL. Workers claim jobs with `SKIP LOCKED` and a lease. Ingest reads headers only,
validates that the directory is one axial series, sorts slices by position and stores the metadata, then queues
an `analyze` job. Analysis streams the pixel data once to build the sagittal plane and QC numbers. The viewer reads
one DICOM file per axial request and windows it server-side into a PNG. Details, diagrams and trade-offs are in
[`docs/design.md`](docs/design.md).

## API

Interactive documentation: **http://localhost:8080/api/docs**.

| Method and path | Purpose |
|---|---|
| `POST /api/jobs` `{"path": "…"}` | Index one directory (interactive) |
| `POST /api/batches` (CSV upload) | One ingest job per CSV row |
| `POST /api/uploads` → `…/files` → `…/complete` | Upload a folder from the browser, then index it |
| `GET /api/jobs`, `GET /api/jobs/{id}`, `POST /api/jobs/{id}/retry` | Job status, reasons, retry |
| `GET /api/batches`, `GET /api/batches/{id}` | Per-batch ready / failed / pending rows |
| `GET /api/series?max_slice_thickness=1.5&study_description=…&q=…` | Cross-study metadata queries |
| `GET /api/series/{id}` | Metadata, analysis status and QC |
| `GET /api/series/{id}/locate?z=…` or `?index=…` | Closest slice to a location |
| `GET /api/series/{id}/axial/{index}?window=400&level=40` | Axial slice as PNG |
| `GET /api/series/{id}/sagittal?window=400&level=40` | Mid-volume sagittal as PNG |
| `GET /api/library/directories` | Directories under `data/` |
| `GET /api/health`, `/api/ready`, `/api/stats` | Liveness, readiness, queue metrics |

## Development

Backend tests (unit tests need nothing; integration tests need a PostgreSQL where the user can create databases):

```bash
cd backend
python -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt
pytest                                                             # unit tests
TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/postgres pytest  # + integration
ruff check . && ruff format --check .
```

Frontend with hot reload, against an API on port 8000:

```bash
cd frontend && npm ci && npm run dev      # http://localhost:5173, proxies /api to localhost:8000
```

Run the API and a worker outside Docker with `uvicorn app.main:app --reload` and `python -m app.worker`,
setting `DATABASE_URL`, `LIBRARY_ROOT`, `UPLOAD_ROOT` and `DERIVED_ROOT`.

### Configuration

| Variable | Default | Meaning |
|---|---|---|
| `DICOM_LIBRARY_DIR` | `./data` | Host folder mounted read-only as the DICOM library |
| `WEB_PORT` | `8080` | Host port of the web app |
| `POSTGRES_PASSWORD` | `ctviewer-dev-only` | Database password (set it in `.env`; see `.env.example`) |
| `WORKER_REPLICAS` | `2` | Number of worker containers |
| `JOB_MAX_ATTEMPTS` | `3` | Attempts for retryable errors |
| `JOB_LEASE_SECONDS` | `120` | Lease before a silent worker's job is reclaimed |
| `WORKER_KINDS` | `ingest,analyze` | Job kinds a worker takes (separate pools per kind) |

### Layout

```
backend/
  app/
    api/            HTTP routes: jobs, batches, uploads, series, health
    dicom/          header reading and validation, HU/windowing, sagittal, analysis
    jobs/           queue (SQL), handlers (ingest, analyze), batches
    worker.py       job worker
    migrate.py      SQL migration runner
    tools/synthetic.py  synthetic CT series generator
  migrations/       schema (001_initial.sql)
  tests/            unit + integration tests
frontend/src/       React app (Viewer, Series, Import & jobs)
docs/               design.md, devops.md
scripts/smoke_test.sh
.github/workflows/ci.yml
```

### Troubleshooting

* **Linux: "Permission denied" reading `data/`.** The containers run as uid 10001 and need read access:
  `chmod -R a+rX data`.
* **Nothing listed under Import.** Check that each series is a sub-folder of `data/` (or of `DICOM_LIBRARY_DIR`)
  and that the stack was started from the repository root.
* **Logs:** `docker compose logs -f api worker`.
