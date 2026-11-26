# Part 1 · System design: CT processing service

Researchers upload a CT study, request processing, and return later to view images and query metadata.
This document describes the target service. The final section distinguishes it from the current Part 2
implementation; target capabilities are not claims that those features already exist.

## Assumptions

* Each directory contains exactly one series: 200–800 DICOM files totaling 100 MB–1 GB. Several directories
  may share a StudyInstanceUID.
* Data is anonymized upstream and used for research. Initially, tens of researchers in one trusted research
  group share a catalog, with thousands of series submitted per month.
* The full service uses institutional OpenID Connect login and HTTPS. Part 2 runs locally without login.
* CSV paths refer to an administrator-configured shared data root readable by the service. A remote server
  cannot read arbitrary paths on a researcher's laptop; upload those directories or copy them to shared storage.
* Accepted sources are immutable and retained with results until explicitly deleted. Shared-storage administrators
  must preserve indexed files. PostgreSQL and source storage use persistent storage and backups.
* The initial viewer targets conventional, consistently oriented axial, single-frame CT with matching dimensions
  and approximately uniform spacing. Arbitrary oblique reconstruction and enhanced multi-frame CT are outside
  this implementation slice. Readable metadata can still be indexed when viewer analysis is unsupported.
* The concrete analysis generates a sagittal plane through the middle of the volume. Other analysis tasks can
  later use the same submission and job lifecycle.

## Architecture

```mermaid
flowchart LR
    B[Browser] -->|HTTPS| W[nginx and React]
    W -->|API requests| A[FastAPI]
    B -->|Signed file uploads| O[(Object storage)]
    A -->|Verify upload completion| O
    A -->|Jobs and outbox transaction| P[(PostgreSQL)]
    A -->|Metadata and result queries| P
    P -->|Pending dispatch| R[Outbox relay and reconciler]
    R -->|Publish job ID| Q[(Redis queue)]
    Q --> K[RQ workers]
    K -->|Read uploaded files| O
    K -->|Read CSV directory| S[(Shared DICOM storage)]
    K -->|Metadata, leases, results, status| P
    A -->|Read axial slice| O
    A -->|Read axial slice| S
```

| Component | Responsibility |
|---|---|
| React and nginx | Directory upload, CSV submission, job history, search and viewer; one public entry point |
| FastAPI | Submissions, upload verification, metadata/status queries and image rendering |
| PostgreSQL | Durable metadata, job history, upload sessions, processing leases and dispatch outbox |
| Outbox relay and reconciler | Publish committed jobs and recover stranded work after outages |
| Redis and RQ workers | Execute slow processing outside HTTP requests; read headers and pixels, generate results |
| Object/shared storage | Retain original DICOM files for workers and image endpoints |

## Submission and processing flows

### Interactive upload

1. The researcher selects a local directory. `POST /api/uploads` creates a durable upload session containing an
   expected file manifest (relative names, sizes and checksums), and returns short-lived signed upload URLs.
2. The browser uploads directly to an isolated object-storage prefix. Persisted per-file completion and multipart
   state support retries. Returning to an incomplete upload may require reselecting the local directory.
3. The researcher requests processing through `POST /api/uploads/{id}/complete`. The API verifies manifest entries,
   sizes and checksums, then seals the source against modification. Partial uploads never enter processing.
   Incomplete sessions expire after 24 hours; cleanup removes their unreferenced objects.
4. Completion creates a job and outbox entry in one transaction. Repeated completion returns the same job.
   Acceptance is acknowledged only after commit. Closing the browser after acceptance cannot cancel processing.

### Shared directory and CSV

`POST /api/jobs` accepts a directory under the shared data root. The Part 2 UI lists these directories.
`POST /api/batches` accepts a UTF-8 CSV: the first column is a path, an optional `path` header is skipped, and blank
rows are ignored. Each nonblank data row creates an independent job and outbox entry, sharing a `batch_id` and
retaining its CSV row number. An invalid directory fails its own job while other rows continue.

Relative paths resolve under the configured root; absolute paths must also resolve inside it. Paths on another
machine are not upload instructions. Both routes resolve to a source reference (shared directory or sealed object
prefix) and invoke the same pipeline.

### Common pipeline

```mermaid
sequenceDiagram
    participant API
    participant DB as PostgreSQL
    participant R as Relay
    participant Q as Redis
    participant W as Worker
    participant S as Source storage
    API->>DB: Commit queued job and outbox entry
    R->>DB: Read pending outbox
    R->>Q: Publish job ID
    R->>DB: Record publication
    Q->>W: Deliver job ID
    W->>DB: Acquire exclusive lease and attempt token
    W->>S: Read DICOM headers
    W->>DB: Commit metadata and link job to series
    W->>S: Read pixels and build middle sagittal plane
    alt Analysis succeeds
        W->>DB: Commit result and done status using attempt token
    else Analysis fails
        W->>DB: Record failed stage and reason using attempt token
    end
    Note over W,DB: Metadata remains searchable after analysis failure
    Note over R,DB: Reconciler recovers stranded dispatch and expired leases
```

Separate header extraction from viewer geometry validation. Save available metadata after identifying a single
series, before pixel decoding or analysis. Missing optional tags are null. Unreadable headers or mixed series
produce an ingestion failure; metadata that cannot be extracted cannot be promised. Unsupported geometry or
corrupt pixels produce an analysis failure while retaining the searchable index.

## Data model and interfaces

* `series`: unique SeriesInstanceUID, StudyInstanceUID, anonymized patient ID, study and series descriptions,
  modality, pixel spacing, slice thickness, header spacing between slices, measured spacing, slice count, rows
  and columns. Store ordered z positions and file references when available. Upserts avoid duplicate metadata
  for repeated submissions of the same immutable series. A series can exist without a successful analysis.
* `jobs`: source reference, batch ID, CSV row number, series link, status (`queued`, `running`, `done`, `failed`),
  processing stage, attempt count, error reason, timestamps, lease expiry and attempt token.
* `results`: successful job/series link and sagittal plane, initially as PostgreSQL `bytea` with dimensions and
  value format. Save the result and mark its job done in one transaction. Later failed jobs do not erase results.
* `upload_sessions`: manifest, object prefix, progress, expiry and accepted job link.
* `job_outbox`: publication requests committed with jobs; publication can safely repeat.

Study fields initially live on the series row. One series per directory does not imply one series per study:
study-level searches deduplicate by StudyInstanceUID. A thickness query uses `slice_thickness < 1.5`, excluding
unknown values. A study-description filter searches StudyDescription; general description search may include
both descriptions. Index thickness and study UID, and paginate catalog and job history so old results remain reachable.

Preserve existing API concepts: `POST /api/jobs`, `POST /api/batches`, `GET /api/jobs`, `GET /api/series`,
`GET /api/series/{id}`, and series `/locate`, `/axial/{index}` and `/sagittal` endpoints. Upload sessions are
full-service additions. Series responses expose analysis availability; image endpoints use the latest successful
result by completion time, or report that analysis is unavailable. Job history retains individual result links.
The browser polls pending work and reloads durable history on return.

## Part 2 viewer behavior

* Sort slices by ImagePositionPatient z, never filename. Index 0 is the lowest z.
* Accept either z in millimetres or a zero-based index. Choose the nearest z; ties select the lower index.
  Out-of-range z selects the nearest endpoint with an explanation; invalid indices are rejected.
* Decode the axial slice and apply its modality transform before windowing. Under the geometry assumptions,
  generate the sagittal plane from the middle column (`columns // 2`) of each slice.
* Display superior slices at the sagittal image's top and mark the selected slice with a horizontal line through
  its row. Label the actual selected z. Use physical spacing for proportions; irregular geometry needs resampling
  or an explicit unsupported-data error.
* Show all ten required entries: patient ID, study description, series description, modality, pixel spacing,
  slice thickness, spacing between slices, number of slices, rows and columns. Label measured spacing separately
  when the header spacing is absent or differs.
* Default window width to 400 and level to 40. Require positive width and apply changes to both views. Store
  analysis values before windowing, so changing the window renders PNGs without rerunning analysis.

Part 2 now validates standard axial orientation, matching dimensions and pixel spacing, aligned in-plane origins,
and approximately uniform z spacing before analysis. It rejects unsupported geometry with a reason while retaining
readable metadata. It does not perform general DICOM reconstruction or resample irregular positions.

## Durability and failures

PostgreSQL is the source of truth and queue delivery is at least once. The full-service contract below requires
application recovery logic in addition to RQ:

* **Dispatch:** the relay publishes committed outbox entries and retries queue outages with backoff. A crash after
  publication but before recording it may duplicate delivery. The reconciler also republishes jobs still queued
  without progress, covering loss of an already-published queue message.
* **Exclusive execution:** atomically acquire a renewable lease with a new attempt token. A live lease prevents
  another delivery from claiming that job. Every metadata, result and status write checks the token and valid
  lease; stale workers cannot commit. Workers stop on renewal failure. Lease expiry can briefly overlap physical
  execution, but only the current attempt may publish changes.
* **Retries:** permanent input errors fail immediately. Transient database/storage errors and interrupted attempts
  retry with backoff, up to three processing attempts, with a 30-minute timeout per attempt. Waiting for queue
  availability consumes no attempt. PostgreSQL stores attempt counts; reconciliation owns retry dispatch so
  independent queue retries cannot reset the budget.
* **Worker interruption:** expired leases are retried or marked failed after the attempt budget. Completed jobs
  are skipped on redelivery. Separate submissions for the same immutable series may run independently; metadata
  upserts are idempotent and each result belongs to its job.
* **Database outage during failure reporting:** callbacks cannot persist while PostgreSQL is unavailable. Once it
  returns, reconciliation finds expired leases and retries or records an interruption failure. Preserve a specific
  cause if available; otherwise state that the worker stopped and the original cause was not recorded.
* **Visible errors:** persist the failed stage and readable reason, such as "Directory not found", "Expected one
  series, found 2", or "Pixel decoding failed". Keep diagnostic traces in protected logs tied to job IDs. Analysis
  failure retains metadata. Terminal outcomes remain visible after browser closure and service restart.

## Choices and trade-offs

| Choice | Rationale and cost |
|---|---|
| PostgreSQL | API and workers write concurrently while researchers query. Transactions, indexes, arrays and binary values fit the workload. SQLite simplifies local deployment but serializes writes; DuckDB suits analytics better than this multi-process transactional service. |
| Redis and RQ with durable dispatch | Keeps processing outside requests and provides process supervision. Outbox, leases and reconciliation add recovery complexity. A PostgreSQL queue could remove Redis but still needs execution supervision and recovery. |
| Direct uploads plus shared paths | Uploads support laptop directories without routing up to 1 GB through the API. Shared paths avoid copying existing institutional data. Two source adapters are needed, and shared-file retention is an operational dependency. |
| Metadata commit before analysis | Failed analysis remains searchable and retryable. Schema and UI must represent metadata without images. |
| Precompute sagittal values | Reads the series once per job and makes viewing cheaper, at the cost of stored output and initial processing time. |
| Small results in PostgreSQL | Result and completion commit atomically. Large or numerous outputs eventually need object storage and cleanup of unreferenced results. |
| Server-rendered PNG | Avoids sending the entire volume to the browser. Window changes require requests and server work; latency must be measured. |

Scale workers independently with bounded concurrency to control disk bandwidth and memory. The sagittal algorithm
retains headers, one decoded slice and the output plane rather than the entire volume; actual memory depends on
image dimensions and decoder buffers. API replicas share persistent storage and PostgreSQL.

At a million series, move results to object storage, add a trigram index for substring searches, use keyset
pagination, and archive or partition job history. Normalize studies if repeated fields become costly, introduce
versioned migrations, and measure before adding replicas or another search service. The
[README](../README.md#why-postgresql) contains the database rationale and scaling discussion required by Part 2.

## Security and operations

The full service uses institutional SSO, accepting identity headers only from a trusted proxy. The initial group
shares access; separate groups would require authorization on metadata, jobs, sources and results. Scope signed
URLs to an upload session and ensure sealing prevents writes using old credentials, for example by copying verified
objects into a worker-readable prefix that upload credentials cannot modify. Validate manifest paths and bound
file counts and total bytes.

Resolve shared paths, including symlinks, within the data root and mount it read-only. Use non-root containers,
managed secrets and HTTPS. Avoid patient metadata and pixels in logs; protect logs and backups as research data.
Test restoration of both PostgreSQL and source files.

Monitor API errors/latency, oldest pending outbox entry, oldest queued job, expired leases, failures by stage,
worker availability and storage capacity. Alert when reconciliation cannot progress. `/api/health` provides
liveness; deployment readiness checks required dependencies. See [devops.md](devops.md) for existing deployment
notes; relay, lease and upload monitoring described here are target additions.

## Requirement review and acceptance scenarios

| Requirement or scenario | Expected outcome in the full service |
|---|---|
| Upload 200–800 files, 100 MB–1 GB | Retain and verify files before accepting processing; incomplete uploads resume or expire |
| Interactive submission and CSV containing valid and invalid paths | Same pipeline; independent durable outcome per CSV data row |
| Thickness below 1.5 mm or given study description | Query indexed metadata, including series whose later analysis failed |
| Close browser after acceptance and return | Processing continues; history and results are retrievable |
| Pixel decoding fails after header extraction | Metadata remains searchable; job shows analysis failure and reason |
| Queue outage or API crash after submission commit | Durable dispatch eventually resumes without losing the accepted job |
| Worker interruption or duplicate delivery | Bounded recovery; stale attempts cannot commit and completed jobs stay completed |
| Database unavailable during failure reporting | Reconciliation retries or records an outcome; missing original cause is stated honestly |
| Select z/index and adjust window/level | Nearest axial image, matching sagittal marker, required metadata, default 400/40 and adjustable windowing |

## Current Part 2 implementation boundary

This inventory describes the current implementation. Backend tests run against real PostgreSQL and cover metadata
retention, the viewer APIs, concurrent claims, stale-attempt rejection, bounded retries and relay crash recovery.
Part 2 reads series from the mounted data folder only. Browser upload to object storage is part of the target
design above, not of the implementation.

| Capability | Current implementation and remaining work |
|---|---|
| Interactive and CSV submission | Mounted folders under `data/`; CSV jobs share a batch ID, but source row numbers are not stored |
| Browser upload | Not implemented: the Part 2 brief only needs a chosen DICOM directory, which the app takes from the mounted `data/` folder. Direct upload to object storage, verification and sealing stay in the target design |
| Metadata queries | Thickness and combined description filters exist; explicit study-only/deduplicated search and pagination remain additions |
| Metadata before analysis | Implemented: headers commit before geometry validation and analysis; output is nullable and the UI displays metadata-only entries |
| Job history and errors | Stored in PostgreSQL with stage and attempt count; listing is limited to the newest 200 jobs without pagination |
| Dispatch/recovery | Implemented: `job_outbox` rows commit with jobs; `python -m app.relay` publishes due rows, republishes queued jobs without progress, and recovers expired leases or attempts past the 30-minute timeout |
| Retries and duplicate protection | Implemented: workers claim a job with a new attempt token and a renewed lease; metadata, result and status writes check both. Retryable database errors back off up to three attempts; RQ retries are not used |
| Results | One sagittal plane currently lives on the series row (latest success wins); job-specific result records are a target addition |
| Viewer | z/index, axial and sagittal views, marker, metadata and adjustable 400/40 windowing exist; geometry validation rejects unsupported orientations and irregular spacing |
| Database deliverables | `backend/schema.sql` contains the schema; `backend/migrations/` upgrades older databases; README contains the PostgreSQL rationale and million-series changes |
| Production operations | SSO, TLS, readiness checks and relay/lease/upload monitoring are design capabilities, not local-app features |

The remaining gaps (browser upload, CSV row numbers, job-specific results, pagination, study-level search, operations) do not
change the architecture; each is an addition to the existing tables and endpoints.
