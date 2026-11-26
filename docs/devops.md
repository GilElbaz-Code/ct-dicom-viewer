# Part 3 · Docker and DevOps

## How the images are built, and why

* **[`backend/Dockerfile`](../backend/Dockerfile)** builds one image for the API, worker and relay; the compose
  `command` picks the role, so they always run the same code and dependencies. It starts from
  `python:3.13-slim` and copies in the `uv` binary. `uv sync --frozen` installs exactly the versions in `uv.lock`
  (reproducible builds). `pyproject.toml` and `uv.lock` are copied before the code, so the dependency layer is
  rebuilt only when dependencies change. The process runs as a non-root user (uid 10001).
* **[`frontend/Dockerfile`](../frontend/Dockerfile)** has two stages: Node builds the React app into static
  files, then only those files are copied into `nginx:alpine`. The runtime image has no Node and no source, just
  nginx and ~1 MB of assets.

## Where the data and the database live

| What | Where | Lifetime |
|---|---|---|
| DICOM files | Host folder `./data` (or `DATA_DIR`), bind-mounted **read-only** at `/data` in `api` and `worker` | Yours; never modified |
| PostgreSQL data | Named volume `pgdata` | Survives `docker compose down`; deleted by `down -v` |
| Schema | `backend/schema.sql`, mounted into `/docker-entrypoint-initdb.d` | Applied once, when `pgdata` is empty |
| Redis queue | Named volume `redisdata`, append-only file on | Queued tasks survive a restart |
| Analysis output (sagittal planes) | In PostgreSQL, on the series row | Same as the database |

The `api`, `worker`, `relay` and `web` containers hold no state, so they can be rebuilt or replaced at any time. In
production the database would be a managed PostgreSQL with backups, Redis a managed instance, and DICOM in object
storage or a network share.

## How the services reach each other

All services share the compose network and find each other by service name:

* The browser talks only to `web`. Its port is published on `127.0.0.1:8080`, because the app has no login.
* `web`'s nginx sends `/api/*` to `http://api:8000` ([`nginx.conf`](../frontend/nginx.conf)), so the browser sees
  a single origin (no CORS). nginx looks `api` up through Docker's DNS (`resolver 127.0.0.11`) on each
  request, so a recreated `api` container with a new IP is found without restarting `web`.
* `api` and `worker` reach PostgreSQL at `db:5432` and Redis at `redis:6379` through `DATABASE_URL` and
  `REDIS_URL`.
* Start order follows the healthchecks: `db` and `redis` must be healthy before the backend services start, and
  `web` waits for `api`'s `/api/health`.

## CI pipeline

The repository has no pipeline, since the app is reviewed by running it locally. On GitHub Actions (or GitLab
CI) I would run these on every pull request:

1. **backend:** `uv sync --frozen`, `ruff check`, `ruff format --check`, then `pytest` with a PostgreSQL
   service container and `TEST_DATABASE_URL` set, so the API tests run as well as the unit tests (which use
   small synthetic DICOM series written with pydicom).
2. **frontend:** `npm ci` and `npm run build`, which type-checks before bundling.
3. **images:** `docker compose build`, so a broken Dockerfile fails the pull request.

On `main`, CD pushes both images to a registry tagged with the commit SHA and deploys that tag to staging
(`helm upgrade --install`). It runs a smoke test there (index a synthetic series, fetch both images), then promotes
the same tag to production. Dependabot or Renovate keeps dependencies current, and Trivy scans the images.

## Deploying on Kubernetes or OpenShift

The compose services map one to one onto a Helm chart, with values per environment (image tag, replicas, hosts):

* `api`, `worker`, `relay` and `web` become Deployments of the same images (one `relay` replica is enough; extra ones are safe). An Ingress (a Route on OpenShift) exposes
  `web` and terminates TLS. Workers autoscale on queue length:
  [`deploy/k8s/worker-autoscaling.yaml`](../deploy/k8s/worker-autoscaling.yaml) is a worker Deployment plus a KEDA
  ScaledObject that targets one worker per two waiting jobs, between 1 and 20 replicas.
* Probes: liveness is `/api/health` for `api` and `/` for `web`. Readiness for `api` needs a separate endpoint
  that also runs `SELECT 1` and a Redis `PING`, because `/api/health` never touches either. Workers renew database leases; the relay recovers jobs whose lease expires.
* PostgreSQL is a managed database or an operator (CloudNativePG), not a hand-written StatefulSet. The schema
  runs as a Job before each release, instead of from initdb.
* Redis is managed, or a StatefulSet with a PersistentVolumeClaim.
* DICOM data comes from a ReadOnlyMany volume (NFS or CephFS) mounted at `/data` in `api` and `worker`.
* Settings live in a ConfigMap. Passwords live in Secrets, synced from Vault by the External Secrets Operator.
  Keycloak login is handled by oauth2-proxy in front of `web`.
* OpenShift runs containers under a random user ID. The backend image already runs as non-root and writes
  nothing to disk, so it works unchanged. The web image would switch to `nginxinc/nginx-unprivileged`, because
  stock nginx needs root to write its cache and PID file.

## Scaling

Every service except PostgreSQL holds no state, so each one scales by adding replicas. Locally:
`docker compose up -d --scale worker=6`.

| Component | How it scales | Where the limit is, and the next step |
|---|---|---|
| `worker` | One job at a time per worker. Jobs are claimed in PostgreSQL (`claim_job` succeeds once), so extra replicas never process a job twice. On Kubernetes, KEDA adds workers as the queue grows. | PostgreSQL connections: each running job opens its own pool (up to 10 connections) in the process RQ forks for it. Above ~20 workers, put PgBouncer in front. Shared DICOM storage bandwidth comes next. |
| `api` | Stateless replicas behind nginx or an Ingress. | Rendering PNGs uses CPU in one Python process per container. Add replicas, or `uvicorn --workers`. Image URLs are versioned (`v=updated_at`), so a CDN could cache them. |
| `relay` | One is enough: each pass publishes up to 100 jobs every 2 s, about 50 jobs per second. | Extra replicas are safe, because `FOR UPDATE SKIP LOCKED` gives each relay different rows. |
| `redis` | Holds only job IDs waiting to be picked up. | Not a bottleneck at this load. Losing it loses nothing, because the relay republishes from the outbox. |
| `db` | Vertical first. A managed PostgreSQL with a read replica for search. | Sagittal planes (~350 KB each) in `bytea` move to object storage at about a million series, and "description contains" search needs a `pg_trgm` index. See the README. |

## Secrets

* Locally, the only secret is `POSTGRES_PASSWORD`. It goes in `.env`, which is git-ignored; `.env.example`
  documents it. Compose has a development default so a clean checkout starts with no setup.
* Images contain no secrets: everything comes from the environment at run time.
* In CI they would be the CI system's encrypted secrets. Deploying to a cloud would use OIDC federation instead of
  long-lived keys.
* In production, secrets come from a secret manager (AWS Secrets Manager, Vault, or Kubernetes Secrets) as
  environment variables or mounted files, and are rotated there.

## Knowing the service is healthy in production

* **Liveness:** `GET /api/health` (the compose healthcheck) and nginx answering on `/`. An external uptime check
  hits the public URL. `/api/health` only shows the process is up and never touches PostgreSQL or Redis. A
  readiness check would also run `SELECT 1` and a Redis `PING`.
* **Work is flowing.** The real signal for this service:
  * Queue depth and the age of the oldest queued task (`rq info`, or Redis metrics).
  * Jobs by status, and the failure ratio over the last hour (`SELECT status, count(*) FROM jobs ...`).
  * Job duration. Alert when the oldest queued job is older than a few minutes, or when failures jump.
* **API:** request rate, p95 latency and 5xx rate per route, from nginx or uvicorn logs into
  Prometheus/Grafana or the cloud provider's monitoring.
* **Resources:** PostgreSQL connections, disk and replication lag; worker memory; disk free on DICOM storage.
* **Logs:** all containers log to stdout (`docker compose logs api worker`); in production they are shipped to a
  central store, with the job id in worker log lines.
