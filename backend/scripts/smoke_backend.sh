#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
TENANT="${TENANT:-aurora_auto}"
PROJECT="${PROJECT:-sales_qa}"
AUTH_TOKEN="${AUTH_TOKEN:-dev-token}"
REQ_ID="smoke-$(date +%s)"
ASSET_KEY_ENC="$(python3 - <<'PY'
from urllib.parse import quote
print(quote("auris/label/event_tags", safe=""))
PY
)"

common_headers=(
  -H "Authorization: Bearer ${AUTH_TOKEN}"
  -H "X-Tenant-Id: ${TENANT}"
  -H "X-Project-Id: ${PROJECT}"
  -H "X-Request-Id: ${REQ_ID}"
)

curl -fsS "${BASE_URL}/healthz" >/tmp/auris_health.json
python3 - <<'PY'
import json
assert json.load(open("/tmp/auris_health.json"))["status"] == "ok"
PY

curl -fsS "${BASE_URL}/readyz" >/tmp/auris_ready.json
python3 - <<'PY'
import json
body=json.load(open("/tmp/auris_ready.json"))
assert body["status"] in {"ok","degraded"}
PY

curl -fsS "${common_headers[@]}" "${BASE_URL}/api/v1/insights/ops-summary?time_range=today" >/tmp/auris_ops.json
python3 - <<'PY'
import json
body=json.load(open("/tmp/auris_ops.json"))
assert body["data"]["metrics"]
assert body["meta"]["trace_id"]
PY

curl -fsS "${common_headers[@]}" "${BASE_URL}/api/v1/audio-sessions/S20250526-000128" >/tmp/auris_session.json
python3 - <<'PY'
import json
body=json.load(open("/tmp/auris_session.json"))
assert body["data"]["audio_session_id"] == "S20250526-000128"
assert body["data"]["evidence_packs"]
PY

curl -fsS "${common_headers[@]}" "${BASE_URL}/api/v1/data-assets/${ASSET_KEY_ENC}" >/tmp/auris_asset.json
python3 - <<'PY'
import json
body=json.load(open("/tmp/auris_asset.json"))
assert body["data"]["asset_key"] == "auris/label/event_tags"
assert body["data"]["trace_id"]
PY

BODY='{"task_version_id":"task_version_v3_2_1","trigger_type":"manual","partition_key":"aurora_auto/BJ-AURORA-001/2025-05-26/12","run_config":{"mode":"smoke"}}'
IDK="${TENANT}:${PROJECT}:task_version_v3_2_1:manual_run:smoke"
curl -fsS "${common_headers[@]}" -H "Idempotency-Key: ${IDK}" -H "Content-Type: application/json" -d "${BODY}" "${BASE_URL}/api/v1/task-runs" >/tmp/auris_run_1.json
curl -fsS "${common_headers[@]}" -H "Idempotency-Key: ${IDK}" -H "Content-Type: application/json" -d "${BODY}" "${BASE_URL}/api/v1/task-runs" >/tmp/auris_run_2.json
python3 - <<'PY'
import json
r1=json.load(open("/tmp/auris_run_1.json"))
r2=json.load(open("/tmp/auris_run_2.json"))
assert r1["data"]["run_id"] == r2["data"]["run_id"]
assert r1["meta"]["trace_id"] == r2["meta"]["trace_id"]
open("/tmp/auris_run_id","w").write(r1["data"]["run_id"])
open("/tmp/auris_trace_id","w").write(r1["meta"]["trace_id"])
PY

BAD='{"task_version_id":"task_version_v3_2_1","trigger_type":"manual","partition_key":"changed"}'
code="$(curl -sS -o /tmp/auris_idem_error.json -w "%{http_code}" "${common_headers[@]}" -H "Idempotency-Key: ${IDK}" -H "Content-Type: application/json" -d "${BAD}" "${BASE_URL}/api/v1/task-runs")"
test "${code}" = "409"
python3 - <<'PY'
import json
body=json.load(open("/tmp/auris_idem_error.json"))
assert body["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"
PY

RUN_ID="$(cat /tmp/auris_run_id)"
TRACE_ID="$(cat /tmp/auris_trace_id)"
curl -fsS "${common_headers[@]}" "${BASE_URL}/api/v1/task-runs/${RUN_ID}" >/tmp/auris_run_detail.json
curl -fsS "${common_headers[@]}" "${BASE_URL}/api/v1/traces/${TRACE_ID}" >/tmp/auris_trace.json
python3 - <<'PY'
import json, os
run=json.load(open("/tmp/auris_run_detail.json"))
trace=json.load(open("/tmp/auris_trace.json"))
assert run["data"]["run_id"] == open("/tmp/auris_run_id").read()
assert trace["data"]["trace_id"] == open("/tmp/auris_trace_id").read()
assert isinstance(trace["data"]["spans"], list)
PY

echo "backend smoke ok"

