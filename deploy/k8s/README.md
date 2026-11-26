# Deploying on Kubernetes or OpenShift

Optional production notes; the assignment runs on docker compose. [`docs/devops.md`](../../docs/devops.md) has the
summary.

The compose services map one to one onto a Helm chart, with values per environment (image tag, replicas, hosts):

* `api`, `worker`, `relay` and `web` become Deployments of the same images (one `relay` replica is enough; extra
  ones are safe). An Ingress (a Route on OpenShift) exposes `web` and terminates TLS.
* Workers autoscale on queue length: [`worker-autoscaling.yaml`](worker-autoscaling.yaml) is a worker Deployment
  plus a KEDA ScaledObject that targets one worker per two waiting jobs, between 1 and 20 replicas.
* Probes: liveness is `/api/health` for `api` and `/` for `web`. Readiness for `api` needs a separate endpoint
  that also runs `SELECT 1` and a Redis `PING`, because `/api/health` never touches either. Workers renew
  database leases; the relay recovers jobs whose lease expires.
* PostgreSQL is a managed database or an operator (CloudNativePG), not a hand-written StatefulSet. The schema
  runs as a Job before each release, instead of from initdb.
* Redis is managed, or a single-primary StatefulSet with a PersistentVolumeClaim (see *Scaling* in devops.md).
* DICOM data comes from a ReadOnlyMany volume (NFS or CephFS) mounted at `/data` in `api` and `worker`.
* Settings live in a ConfigMap. Passwords live in Secrets, synced from Vault by the External Secrets Operator.
  Keycloak login is handled by oauth2-proxy in front of `web`.
* OpenShift runs containers under a random user ID. The backend image already runs as non-root and writes
  nothing to disk, so it works unchanged. The web image would switch to `nginxinc/nginx-unprivileged`, because
  stock nginx needs root to write its cache and PID file.
