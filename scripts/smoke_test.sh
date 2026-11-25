#!/usr/bin/env bash
# End-to-end check against a running stack: index a series, wait for analysis, fetch images,
# submit a batch with a bad row. Used by CI; safe to run locally after `docker compose up`.
#
#   scripts/smoke_test.sh [series_path]     (default: synthetic_chest, from the demo-data tool)
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8080}"
SERIES_PATH="${1:-synthetic_chest}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

json() { python3 -c "import json, sys; print(json.load(sys.stdin)$1)"; }
fail() { echo "SMOKE TEST FAILED: $*" >&2; exit 1; }

echo "waiting for $BASE/api/ready"
for _ in $(seq 1 60); do
  curl -fsS "$BASE/api/ready" >/dev/null 2>&1 && break
  sleep 2
done
curl -fsS "$BASE/api/ready" >/dev/null || fail "API not ready"
curl -fsS "$BASE/" | grep -q "CT DICOM Viewer" || fail "web app not served"

echo "indexing $SERIES_PATH"
job_id=$(curl -fsS -X POST "$BASE/api/jobs" -H 'Content-Type: application/json' \
  -d "{\"path\": \"$SERIES_PATH\"}" | json '["id"]')

status=queued
for _ in $(seq 1 60); do
  status=$(curl -fsS "$BASE/api/jobs/$job_id" | json '["status"]')
  [[ "$status" == succeeded || "$status" == failed ]] && break
  sleep 1
done
[[ "$status" == succeeded ]] || fail "ingest job $job_id ended as $status: $(curl -fsS "$BASE/api/jobs/$job_id")"
series_id=$(curl -fsS "$BASE/api/jobs/$job_id" | json '["result"]["series_id"]')

series_status=indexed
for _ in $(seq 1 120); do
  series_status=$(curl -fsS "$BASE/api/series/$series_id" | json '["status"]')
  [[ "$series_status" != indexed ]] && break
  sleep 1
done
[[ "$series_status" == ready ]] || fail "series $series_id is $series_status"

index=$(curl -fsS "$BASE/api/series/$series_id/locate?z=0" | json '["index"]')
curl -fsS -o "$TMP/axial.png" "$BASE/api/series/$series_id/axial/$index?window=400&level=40"
curl -fsS -o "$TMP/sagittal.png" "$BASE/api/series/$series_id/sagittal?window=1500&level=-600"
for f in axial sagittal; do
  [[ "$(head -c 4 "$TMP/$f.png" | tail -c 3)" == "PNG" ]] || fail "$f image is not a PNG"
done

printf 'path\n%s\ndoes/not/exist\n' "$SERIES_PATH" > "$TMP/batch.csv"
rejected=$(curl -fsS -F "file=@$TMP/batch.csv" "$BASE/api/batches" | json '["rejected"]')
[[ "$rejected" == 1 ]] || fail "expected 1 rejected batch row, got $rejected"

thin=$(curl -fsS "$BASE/api/series?max_slice_thickness=100" | json '["total"]')
[[ "$thin" -ge 1 ]] || fail "metadata query returned nothing"

echo "smoke test passed: job $job_id -> series $series_id (slice $index at z=0)"
