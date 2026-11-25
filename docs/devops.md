# Part 3 · Docker and DevOps

## How the images are built, and why

**Backend** ([`backend/Dockerfile`](../backend/Dockerfile)): one image for every Python role. The command picks
the role: API (`uvicorn`), worker (`python -m app.worker`), migrations (`python -m app.migrate`) or the demo-data
tool. One image means one build, one scan and one version tag, and the worker can never run different code
from the API.

* **Multi-stage.** The `build` stage installs the pinned `requirements.txt` into a virtualenv; `runtime` copies
  only that virtualenv onto a fresh `python:3.13-slim`, so pip caches and build files never reach production.
* **Layer order for caching.** Requirements are copied and installed *before* the source code, so editing
  Python rebuilds in seconds and only a dependency change reinstalls packages.
* **Non-root.** The runtime runs as uid 10001. `/data/uploads` and `/data/derived` are created and owned by
  that user in the image, so the named volumes mounted there inherit the right ownership.
* **A `test` stage** adds pytest and ruff to the runtime image. CI lints and tests *inside the image that
  ships*, so "works in CI" means "works in the container".
* All Python dependencies are pinned and all have prebuilt wheels for linux/amd64 and linux/arm64 (Apple
  Silicon), so builds need no compiler and are reproducible.

**Frontend** ([`frontend/Dockerfile`](../frontend/Dockerfile)): `node:22-slim` runs `npm ci` (exact versions from
`package-lock.json`) and `vite build`; the final image is `nginx:alpine` with only the static files and
[`nginx.conf`](../frontend/nginx.conf). Node and `node_modules` never reach production; the image is ~50 MB.

## Where the data and the database live

Containers are disposable; nothing that matters is stored in a container's own filesystem.

| Data | Where | Mounted into | Access |
|---|---|---|---|
| Source DICOM library | Host folder `./data` (or `DICOM_LIBRARY_DIR`); in production the research NAS | api, worker | **read-only** |
| Browser uploads | Named volume `uploads` | api (rw), worker (ro) | only the API writes |
| Derived results (sagittal planes, QC) | Named volume `derived` | worker (rw), api (ro) | only workers write |
| PostgreSQL data | Named volume `pgdata` | db | only Postgres |

The read-only mounts are a guarantee, not a convention: no bug in the app can modify source data. Each volume
has a single writer. `docker compose down` keeps all volumes; `down -v` deletes them for a clean start. In
production the database would be a managed or dedicated Postgres with backups and point-in-time recovery,
and `derived` would be on shared storage or an object store so workers can run on several hosts.

## How the services reach each other

Compose puts every service on one private network and resolves **service names** through Docker's DNS: the
API connects to `db:5432`, nginx proxies `/api/` to `api:8000`. **Only `web` publishes a port**: 8080 on the host,
bound to `127.0.0.1` by default because the demo has no login (`WEB_BIND=0.0.0.0` opens it to the network). Postgres and the API are not reachable from outside, so the database needs no public exposure at all.

* The browser talks only to nginx (same origin for the app and `/api`, so there is no CORS to configure).
* nginx resolves `api` per request (`resolver 127.0.0.11`), so a recreated API container with a new IP is
  picked up without restarting nginx. It also overwrites `X-Forwarded-User`, so a browser cannot claim an
  identity. In production the SSO check runs *in* this nginx (`auth_request` to oauth2-proxy), and the same
  line sets the header from the verified session instead of clearing it.
* Workers never talk to the API: they coordinate only through Postgres (job rows) and the shared volumes.
  That is why `docker compose up --scale worker=5` just works.
* Start order is explicit: `db` healthy → `migrate` (one-shot) completed successfully → `api` and `worker` →
  `web` once the API is healthy. Migrations run once per deploy instead of racing inside each replica (a
  Postgres advisory lock makes concurrent runs safe anyway).

## CI pipeline

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml), on every pull request and push to `main`:

1. **backend**: build the image's `test` stage (layer cache in GitHub Actions), run `ruff` and `pytest`
   inside it against a Postgres service container. The integration tests create a throw-away database per test,
   run the real migrations, and drive the API, the queue and the worker together.
2. **frontend**: `npm ci` and `npm run build`, which type-checks with `tsc` and bundles.
3. **e2e** (after both pass): `docker compose up --build` on a clean runner, the same way a reviewer would,
   generate a synthetic series, and run [`scripts/smoke_test.sh`](../scripts/smoke_test.sh): index, wait
   for analysis, fetch axial and sagittal PNGs, submit a batch with a bad row, run a metadata query. On failure
   it prints the container logs.
4. **publish** (`main` only): push `backend` and `web` images to GitHub Container Registry, tagged with the
   commit SHA (immutable, used for deploys and rollbacks) and `latest`.

Deploying would be one more job: on a tag, update the image tags in the target environment and run
`migrate` before the new API and worker replicas start. Rollback = redeploy the previous SHA; migrations are
written to be backward-compatible (add columns first, remove them a release later).

## Secrets

* **Nothing secret is in the repository or the images.** Compose reads `POSTGRES_PASSWORD` from the
  environment or an untracked `.env` (`.env.example` documents the variables; `.gitignore` excludes `.env`).
  The default (`ctviewer-dev-only`) exists so a reviewer can run the stack with zero setup, and is only valid
  on the private compose network.
* **In production** the password comes from the platform's secret store (Docker/Kubernetes secrets, Vault,
  AWS Secrets Manager) mounted as a file. The app already supports this: `POSTGRES_PASSWORD_FILE`
  ([`backend/app/config.py`](../backend/app/config.py)), the same convention the official Postgres image uses.
* **In CI** the registry login uses the per-run `GITHUB_TOKEN`, which expires with the job, so no long-lived
  credential is stored. The test database password is a throw-away value that exists only inside the runner.
* Rotate in two steps, because `POSTGRES_PASSWORD` is only read when the database volume is first created:
  `ALTER ROLE ctviewer PASSWORD '…'` in Postgres, then update the secret and restart `api` and `worker`.
  No image rebuild is needed. The app builds its connection string with psycopg's `make_conninfo`, so any
  characters are safe in the password.

## Knowing the service is healthy in production

| Signal | Source | Alert when |
|---|---|---|
| API process alive | `GET /api/health`, Docker healthcheck in compose | container unhealthy → restart (orchestrator) |
| API can serve | `GET /api/ready` checks the database and the mounted volumes; 503 otherwise | load balancer removes the replica; page if all fail |
| Workers alive | each worker touches a heartbeat file every loop; compose healthcheck fails if it is older than 60 s | worker unhealthy for > 2 min |
| Queue is moving | `GET /api/stats` → `oldest_queued_seconds` | oldest runnable job older than ~10 min: workers down or saturated |
| Jobs are healthy | `/api/stats` → `failed_last_hour`, `expired_leases` | failure rate spike (bad data or a broken dependency); expired leases mean workers are crashing |
| Errors in context | JSON logs on stdout (job id, worker id, error code), collected by Loki/ELK | error-level log rate |
| Capacity | container CPU/memory (cAdvisor), Postgres connections and disk, volume free space | disk > 80%, memory near the limit (1 GB series) |

In a full deployment `/api/stats` would be exposed as Prometheus metrics (`/metrics`) and scraped, with the
alerts above in Alertmanager and a dashboard of queue depth, job duration percentiles and failures by
`error_code`. A synthetic check (the smoke test against a known small series, every few minutes) catches
failures that no single component reports.
