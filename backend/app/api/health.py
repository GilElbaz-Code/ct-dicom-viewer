from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.api.deps import AppSettings, Conn
from app.jobs import queue

router = APIRouter(tags=["health"])


@router.get("/health")
def liveness() -> dict:
    """The process is up. Used by the container healthcheck; does not touch dependencies."""
    return {"status": "ok"}


@router.get("/ready")
def readiness(request: Request, settings: AppSettings) -> JSONResponse:
    """Ready to serve: database reachable and data volumes mounted."""
    checks = {}
    try:
        # Not the Conn dependency: getting a connection is itself the check, and it must fail fast (503), not
        # wait out the pool's 30 s default and surface as a 500.
        with request.app.state.pool.connection(timeout=2) as conn:
            conn.execute("SELECT 1").fetchone()
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["database"] = f"error: {exc}"
    for name, path in (
        ("library", settings.library_root),
        ("uploads", settings.upload_root),
        ("derived", settings.derived_root),
    ):
        checks[name] = "ok" if path.is_dir() else f"missing: {path}"
    ok = all(v == "ok" for v in checks.values())
    return JSONResponse({"status": "ok" if ok else "degraded", "checks": checks}, status_code=200 if ok else 503)


@router.get("/stats")
def stats(conn: Conn) -> dict:
    """Numbers worth alerting on: queue depth, oldest waiting job, failure rate."""
    series = conn.execute("SELECT status, count(*) AS n FROM series GROUP BY status").fetchall()
    return {**queue.stats(conn), "series_by_status": {r["status"]: r["n"] for r in series}}
