# CT DICOM Viewer

Home assignment for the Python Backend Engineer position, Samueli Integrative Cancer Pioneering Institute.

Index CT DICOM series (one folder at a time or a CSV batch), follow the jobs, query the metadata across studies,
and view any series as an axial slice at a chosen location next to a sagittal view with that location marked.

| Part | Where |
|---|---|
| 1. System design | [`docs/design.md`](docs/design.md) |
| 2. Implementation | [`backend/`](backend) (FastAPI, RQ worker, `schema.sql`), [`frontend/`](frontend) (React + Mantine), [`docker-compose.yml`](docker-compose.yml) |
| 3. Docker and DevOps | [`docs/devops.md`](docs/devops.md) |

![Viewer: axial slice, sagittal view with the slice marked, series details](docs/images/viewer.png)

## Run it from scratch

Needs only Docker with Compose v2 (Docker Desktop, or Docker Engine 24+).

1. Clone the repository. Download the two sample series (the assignment's shared Google Drive folder,
   "Sample DICOM series for the exam") and extract them into `data/` so that each series is its own top-level
   folder:

   ```
   data/
   ├── 0x23D38C868F3ED85E20/   *.dcm
   └── 0xE13CE7B52F313D3C82/   *.dcm
   ```

   A folder holding both series fails with "Expected one series per directory, found 2"; index each series
   folder instead.

2. Start everything:

   ```bash
   docker compose up --build -d
   ```

3. Open **http://localhost:8080**. On **Jobs**, click **Index** next to each folder, or upload
   [`examples/sample_batch.csv`](examples/sample_batch.csv). Each series takes a few seconds.
   [`examples/batch_with_errors.csv`](examples/batch_with_errors.csv) shows how failed rows are reported.
4. On **Viewer**, pick a series and enter a location in mm or a slice index. Change the window with the presets
   or the width/level fields (default soft tissue, 400/40). **Search** runs queries across studies.

Stop with `docker compose down`. Add `-v` to also delete the database.

## Why PostgreSQL

Several processes write at the same time (the API creates jobs, every worker saves results), while researchers
query. PostgreSQL handles concurrent writers natively; SQLite serializes all writes and DuckDB allows one writing
process. It also gives arrays (slice positions), `bytea` (the analysis output) and transactions that save a
series and mark its job done together. The schema is [`backend/schema.sql`](backend/schema.sql), loaded by
Postgres when its volume is first created.

**At a million series**, the `series` table itself is small for PostgreSQL. What I would change:

* Move the sagittal images (~350 KB each, ~350 GB in total) out of the database into object storage (S3/MinIO),
  keeping only the key in the row.
* Add a `pg_trgm` GIN index for "description contains" searches, which today scan the table. Add keyset
  pagination, since the API returns at most 500 rows. Elasticsearch/OpenSearch only if search grows into free
  text or facets over many header fields.
* Split studies into their own table and keep extra header fields in an indexed JSONB column, so new query fields
  need no migration. Adopt a migration tool (Alembic or plain numbered SQL files) instead of a single init script.
* Partition or archive the `jobs` table by month; put PgBouncer in front and a read replica behind the search page.
* For population-level analytics, export to Parquet nightly and query it with DuckDB.

## How it works

`web` (nginx) serves the React app and forwards `/api` to `api` (FastAPI). Submitting a folder, or each row of a
CSV, inserts a row in `jobs` and puts a task on a Redis queue. An RQ `worker` takes the task, reads the DICOM
headers, checks the folder holds one series, and sorts the slices by position. It commits the header metadata
before validating viewer geometry and reading pixels to build the sagittal plane
(the "analysis"). The output and successful job status commit together. If analysis fails, metadata remains
searchable and the viewer shows that analysis is unavailable; an earlier successful result is retained. A
problem with the data fails the job at once, with the reason. Database errors, a killed job process and a dead
worker are retried twice before the job fails with the reason. A job that already finished is skipped if it is
delivered again ([design.md, *Jobs and failures*](docs/design.md#jobs-and-failures)). The viewer reads one DICOM
file per axial image and windows it into a PNG on the server.

The backend has three layers, and each only calls the one below it:

| Layer | Folder | Rule |
|---|---|---|
| API | `backend/app/api/` | HTTP only: validate input, call one service, return a model or a PNG |
| Logic | `backend/app/services/` | What the app does; no FastAPI, no SQL |
| Data | `backend/app/db/` | Every SQL statement; each function is one transaction |

### Code reading order

| File | What it does |
|---|---|
| `backend/schema.sql` | The two tables: `series` (metadata, slice positions, analysis output) and `jobs` |
| `backend/app/services/dicom.py` | Read and validate a folder's headers; HU conversion; sagittal; window/level to PNG |
| `backend/app/services/ingest.py` | The background job: claim (skips duplicates) → path check → headers → sagittal → save |
| `backend/app/services/jobs.py` | Submit folders or a CSV; `publish` puts a job ID on RQ (called by the relay) |
| `backend/app/relay.py`, `db/dispatch.py` | Outbox relay: publish committed jobs, recover expired leases |
| `backend/app/db/repository.py` | All SQL |
| `backend/app/services/viewer.py` | Closest slice to a location; axial and sagittal PNGs |
| `backend/app/api/*.py`, `main.py` | Routes, response models, error → HTTP status |
| `backend/app/worker.py` | The RQ worker class; interrupted attempts are recovered by the relay from their database lease |
| `docker-compose.yml` | db, redis, api, worker, relay, web |
| `frontend/src/api.ts` | Typed calls to the API |
| `frontend/src/pages/Viewer.tsx`, `Jobs.tsx`, `Search.tsx` | The three screens |

## API

Interactive documentation: **http://localhost:8080/api/docs**.

| Method and path | Purpose |
|---|---|
| `GET /api/directories` | Folders in the data directory |
| `POST /api/jobs` `{"path": "…"}` | Index one folder |
| `POST /api/batches` (CSV upload) | Index every folder listed in a CSV; one job per row |
| `GET /api/jobs?batch_id=` | Jobs, newest first, with the reason for each failure |
| `GET /api/series?max_slice_thickness=1.5&description=chest` | Query across studies |
| `GET /api/series/{id}` | Series metadata |
| `GET /api/series/{id}/locate?z=…` or `?index=…` | Closest slice to a location in mm, or the slice at an index |
| `GET /api/series/{id}/axial/{index}?window=400&level=40` | Axial slice as PNG |
| `GET /api/series/{id}/sagittal?window=400&level=40` | Mid-volume sagittal as PNG |
| `GET /api/health` | Liveness |

Slice index 0 is the most inferior slice (lowest z). Slices are always ordered by `ImagePositionPatient` z,
never by file name. A location outside the scan shows the nearest slice and says so. *Spacing between slices*
shows the header tag next to the spacing measured from the positions. Many series leave the tag out, and the
PET/CT sample writes 3.27 mm while its slices are 2.5 mm apart.

**CSV format:** one folder per row in the first column, relative to `data/`; a first row reading `path` is
skipped.

## Development

```bash
cd backend
uv sync                                   # Python 3.13+ and dependencies into .venv
uv run pytest                             # unit tests
TEST_DATABASE_URL=postgresql://postgres:pw@localhost:5432/postgres uv run pytest   # + API tests (drops tables!)
uv run ruff check . && uv run ruff format --check .
uv run uvicorn app.main:app --reload      # API on :8000 (needs DATABASE_URL, REDIS_URL, DATA_DIR)
uv run rq worker -w app.worker.Worker     # worker (Linux/macOS; RQ needs fork)
uv run python -m app.relay                # relay: without it jobs stay queued

cd frontend
npm ci && npm run dev                     # UI on http://localhost:5173, forwards /api to :8000
```

| Variable | Default | Meaning |
|---|---|---|
| `DATA_DIR` | `./data` | Host folder with the DICOM series, mounted read-only at `/data` |
| `POSTGRES_PASSWORD` | `ctviewer` | Set it in `.env` before the first start (see `.env.example`) |
| `WEB_PORT` | `8080` | Port of the web app, bound to localhost (the app has no login) |

**Troubleshooting.** On Linux the containers run as uid 10001 and need read access to the data
(`chmod -R a+rX data`). For an existing database, apply the migrations below instead of deleting its volume.
Logs: `docker compose logs -f api worker relay`.


### Updating an existing database

Fresh installations use `backend/schema.sql`. Before running the updated API/workers against an existing database,
stop those processes, back up PostgreSQL, and apply the files in `backend/migrations/` in order. `001` removes the
sagittal output's NOT NULL constraint; `002` adds the outbox and lease columns, and requeues
jobs left running. Neither deletes existing series or results.
From PowerShell, using the database credentials configured in Compose:

```powershell
Get-ChildItem backend/migrations/*.sql | Sort-Object Name | ForEach-Object { Get-Content -Raw $_ | docker compose exec -T db sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' }
```

Rebuild/restart the API, workers, relay and web app together after the migrations. The series response now includes
`analysis_available`; metadata can exist without an image. No migration is applied automatically at API startup.

### Backend conventions and supported data

Use explicit keyword arguments for application function calls, such as `get_series(series_id=series_id)`.
Keep positional arguments where Python or a third-party API requires them (including the RQ task invocation).
Services use typed records, the HTTP layer uses Pydantic response models, and SQL stays in the repository.
Ruff checks imports, common errors and application function annotations; `ruff format` maintains formatting.
Database failures are retried from PostgreSQL attempt counts; data problems fail the job at once with a readable error.
The API closes its database pool on shutdown.

The current reconstruction supports single-frame MONOCHROME2 CT in standard axial orientation, matching image
sizes and pixel spacing, aligned in-plane origins, and approximately uniform z spacing. It rejects unsupported
geometry with a reason rather than displaying a distorted sagittal reconstruction. Metadata remains searchable.
See `docs/design.md` for which parts of the full-service design are implemented.
