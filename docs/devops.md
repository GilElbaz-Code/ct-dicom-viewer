# Part 3 · Docker and DevOps

## How the images are built, and why

* **[`backend/Dockerfile`](../backend/Dockerfile)** builds one image for the API, worker and relay; the compose
  `command` picks the role, so they always run the same code and dependencies. It starts from
  `python:3.13-slim` and copies in the `uv` binary. `uv sync --frozen` installs exactly the versions in `uv.lock`
  (locked Python dependencies). Base image tags can change; production builds would pin image digests as well.
  `pyproject.toml` and `uv.lock` are copied before the code, so the dependency layer is
  rebuilt only when dependencies change. The process runs as a non-root user (uid 10001).
* **[`frontend/Dockerfile`](../frontend/Dockerfile)** has two stages: Node builds the React app into static
  files, then only those files are copied into `nginx:alpine`. The runtime image has no Node and no source, just
  nginx and ~1 MB of assets.

## Where the data and the database live

| What | Where | Lifetime |
|---|---|---|
| DICOM files | Host folder `./data` (or `DATA_DIR`), bind-mounted **read-only** at `/data` in `api` and `worker` | Yours; never modified |
| PostgreSQL data | Named volume `pgdata` | Survives `docker compose down`; deleted by `down -v` |
| Schema | `backend/schema.sql`, mounted into `/docker-entrypoint-initdb.d` | Applied once, when `pgdata` is empty; an existing database is upgraded with `backend/migrations/` (see the README) |
| Redis queue | Named volume `redisdata`, append-only file on | Persistence supports restart recovery; PostgreSQL outbox/leases recover work if Redis loses data |
| Analysis output (sagittal planes) | In PostgreSQL, on the series row | Same as the database |

The `api`, `worker`, `relay` and `web` containers hold no durable application state. Replacing workers may interrupt
an attempt; leases and retries recover it. In
production the database would be a managed PostgreSQL with backups, Redis a managed instance, and DICOM in object
storage or a network share.

## How the services reach each other

All services share the compose network and find each other by service name:

* The browser talks only to `web`. Its port is published on `127.0.0.1:8080`, because the app has no login.
* `web`'s nginx sends `/api/*` to `http://api:8000` ([`nginx.conf`](../frontend/nginx.conf)), so the browser sees
  a single origin (no CORS). nginx resolves `api` through Docker's DNS (`resolver 127.0.0.11`), caching the result
  for ten seconds, so a recreated container's new IP can be found without restarting `web`.
* `api`, `worker` and `relay` reach PostgreSQL at `db:5432` and Redis at `redis:6379` through `DATABASE_URL` and
  `REDIS_URL`, set once in the compose file's shared backend block.
* Start order follows the healthchecks: `db` and `redis` must be healthy before the backend services start, and
  `web` waits for `api`'s `/api/health`.

## CI pipeline

I would use GitHub Actions with three parallel jobs on every push and pull request:

1. **backend:** `uv sync --frozen`, `ruff check`, `ruff format --check`, then `pytest` with a PostgreSQL
   service container and `TEST_DATABASE_URL` set, so the API tests run as well as the unit tests (which use
   small synthetic DICOM series written with pydicom).
2. **frontend:** `npm ci` and `npm run build`, which type-checks before bundling.
3. **e2e:** the README's "run it from scratch" on a clean runner. It writes a small synthetic series into
   `data/`, runs `docker compose up --build --wait`, then goes through nginx: indexes the series, waits for the
   job to finish, checks that the thinner-than-1.5 mm query finds it and that a location picks the right slice,
   and fetches both PNGs. A broken Dockerfile, compose file or service wiring fails the pull request.

This pipeline is a proposal; a GitHub Actions workflow is not included in the submission.

For a deployed service, CD on `main` would push both images to a registry tagged with the commit
SHA and deploy that tag to staging. The same smoke test runs there, then the same tag
is promoted to production. Dependabot or Renovate keeps dependencies current, and Trivy scans the images.

## Scaling

`api`, `worker`, `relay` and `web` hold no state, so they scale by adding replicas. Locally:
`docker compose up -d --scale worker=6`. PostgreSQL and Redis hold state and do not: each is one primary that
scales vertically, or becomes a managed service.

| Component | How it scales | Where the limit is, and the next step |
|---|---|---|
| `worker` | One job at a time per worker. Only one attempt holds a valid PostgreSQL lease; stale attempts cannot commit. Retries can execute a job again, and lease expiry can briefly overlap physical execution. | Each job process has a pool capped at 10 connections. Budget connections across API, workers and relay before increasing replicas; add PgBouncer if needed. Shared storage bandwidth also limits throughput. |
| `api` | Stateless replicas behind nginx. | PNG rendering uses CPU. Browsers may reuse images for five minutes under `Cache-Control: private, max-age=300`; shared caches must not store them. Requests include `v=updated_at`. A stale request reaching the API gets 409 with `no-store`, triggering a viewer refresh. This does not purge previously cached images. |
| `relay` | A pass handles up to 100 jobs using batched Redis operations: status lookup followed by enqueueing when needed. Full passes run back to back; otherwise it polls every two seconds. | `FOR UPDATE SKIP LOCKED` separates concurrently selected batches. Delivery can repeat, so worker claims and fencing remain necessary. |
| `redis` | One primary stores RQ job records and queue IDs with append-only persistence. Replicas can support failover. | If Redis loses data, the durable PostgreSQL outbox republishes queued work and leases recover interrupted attempts. RQ-only history may be lost. Monitor queue depth, memory and persistence health. |
| `db` | Vertical first. A managed PostgreSQL with a read replica for search. | Sagittal planes (float32, ~700 KB each) in `bytea` move to object storage at about a million series, while "description contains" search already has `pg_trgm` indexes. See the README. |

## Secrets

* Locally, the only secret is `POSTGRES_PASSWORD`. It goes in `.env`, which is git-ignored; `.env.example`
  documents it. Compose has a development default so a clean checkout starts with no setup.
* Images contain no secrets: everything comes from the environment at run time.
* CI would need none: its PostgreSQL is a throwaway service container. Registry or cloud credentials would be the
  CI system's encrypted secrets. Deploying to a cloud would use OIDC federation instead of
  long-lived keys.
* In production, secrets come from a secret manager (AWS Secrets Manager or Vault) as
  environment variables or mounted files, and are rotated there.

## Knowing the service is healthy in production

* **Liveness:** `GET /api/health` (the compose healthcheck) and nginx answering on `/`. An external uptime check
  hits the public URL. `/api/health` only shows the process is up and never touches PostgreSQL or Redis.
  A readiness check would query PostgreSQL: submissions remain durable during Redis outages. Redis availability
  would be monitored separately for workers and relay, along with their progress.
* **Work is flowing.** The real signal for this service:
  * Queue depth and the age of the oldest queued task (`rq info`, or Redis metrics).
  * Jobs by status, and the failure ratio over the last hour (`SELECT status, count(*) FROM jobs ...`).
  * Job duration. Alert when the oldest queued job is older than a few minutes, or when failures jump.
* **API:** request rate, p95 latency and 5xx rate per route, from nginx or uvicorn logs into
  Prometheus/Grafana or the cloud provider's monitoring.
* **Resources:** PostgreSQL connections, disk and replication lag; worker memory; disk free on DICOM storage.
* **Logs:** all containers log to stdout (`docker compose logs api worker`); in production they are shipped to a
  central store, with the job id in worker log lines.
