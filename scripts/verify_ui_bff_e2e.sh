#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"
DEFAULT_COMPLETION_HMAC_VALUE="auris-e2e-completion-secret-32chars-minimum"
DEFAULT_PLAYBACK_GRANT_HMAC_VALUE="auris-e2e-playback-grant-secret-32chars-minimum"
DEFAULT_CALLBACK_HMAC_VALUE="auris-e2e-callback-key-material-32chars-minimum"

if [ -n "${PYTHON:-}" ]; then
  PYTHON_BIN="${PYTHON}"
elif [ -x "${ROOT}/backend/.venv/bin/python" ]; then
  PYTHON_BIN="${ROOT}/backend/.venv/bin/python"
else
  PYTHON_BIN="$(command -v python3)"
fi

COMPLETION_HMAC_KEY_ID="${AURIS_E2E_COMPLETION_HMAC_KEY_ID:-auris-e2e-completion}"
COMPLETION_HMAC_VALUE="${AURIS_E2E_COMPLETION_HMAC_SECRET:-${COMPLETION_RECEIPT_SECRET:-${DEFAULT_COMPLETION_HMAC_VALUE}}}"
COMPLETION_KEY_BINDINGS_JSON="$(
  "${PYTHON_BIN}" - "${COMPLETION_HMAC_KEY_ID}" "${COMPLETION_HMAC_VALUE}" <<'PY'
import json
import sys

key_id, secret = sys.argv[1:]
print(
    json.dumps(
        {
            key_id: {
                "secret": secret,
                "allowed_sources": ["dagster", "object_storage", "external_callback"],
                "allowed_scopes": [
                    {"tenant_id": "aurora_auto", "project_id": "sales_qa"}
                ],
            }
        },
        separators=(",", ":"),
    )
)
PY
)"
CALLBACK_HMAC_KEY_ID="${AURIS_E2E_CALLBACK_HMAC_KEY_ID:-auris-e2e-callback}"
CALLBACK_HMAC_VALUE="${AURIS_E2E_CALLBACK_SHARED_TOKEN:-${DEFAULT_CALLBACK_HMAC_VALUE}}"
CALLBACK_KEY_BINDINGS_JSON="$(
  "${PYTHON_BIN}" - "${CALLBACK_HMAC_KEY_ID}" "${CALLBACK_HMAC_VALUE}" <<'PY'
import json
import sys

key_id, key_material = sys.argv[1:]
print(
    json.dumps(
        {key_id: {"secret": key_material, "state": "active"}},
        separators=(",", ":"),
    )
)
PY
)"

E2E_URL="${AURIS_E2E_URL:-http://127.0.0.1:5173/}"
AUTOSTART="${AURIS_E2E_AUTOSTART:-1}"
FORCE_AUTOSTART="${AURIS_E2E_FORCE_AUTOSTART:-0}"
REAL_STACK="${AURIS_REAL_STACK_E2E:-${AURIS_E2E_REAL_STACK:-0}}"
COMPOSE_FILE="${ROOT}/docker/local/docker-compose.yml"
ARTIFACT_SUFFIX="${AURIS_E2E_ARTIFACT_SUFFIX:-$(date +%s)-$$}"
E2E_RUN_ID="${AURIS_E2E_RUN_ID:-e2e-${ARTIFACT_SUFFIX}}"
RESULT_PATH="${AURIS_E2E_RESULT_PATH:-${ROOT}/prototype/auris-flow-ui/e2e/artifacts/platform-bff-result-${ARTIFACT_SUFFIX}.json}"
OUTBOX_RESULT_PATH="${AURIS_E2E_OUTBOX_RESULT_PATH:-${ROOT}/prototype/auris-flow-ui/e2e/artifacts/outbox-dispatch-result-${ARTIFACT_SUFFIX}.json}"
export QDRANT_API_KEY=${QDRANT_API_KEY:-auris-qdrant-test-key}
TMP_DIR=""
BFF_PID=""
UI_PID=""
WORKER_PID=""
WORKER_HEALTH_PATH=""
DAGSTER_PID=""
CALLBACK_PID=""
REAL_STACK_DB_NAME=""
MIGRATION_DB_URL=""
MINIO_BOOTSTRAP_TIMEOUT_SECONDS="${AURIS_MINIO_BOOTSTRAP_TIMEOUT_SECONDS:-60}"

run_minio_bootstrap() {
  local container_name status
  container_name="auris-flow-minio-bootstrap-$(date +%s)-$$"
  if "${PYTHON_BIN}" "${ROOT}/scripts/run_with_deadline.py" \
    --timeout-seconds "${MINIO_BOOTSTRAP_TIMEOUT_SECONDS}" \
    --label "MinIO bootstrap" -- \
    docker compose -f "${COMPOSE_FILE}" run --name "${container_name}" --rm --no-deps minio-bootstrap; then
    return 0
  else
    status=$?
  fi
  if [ "${status}" -eq 124 ]; then
    docker rm --force "${container_name}" >/dev/null 2>&1 || true
  fi
  return "${status}"
}

cleanup() {
  local status=$?
  if [ "${status}" -ne 0 ] && [ -n "${TMP_DIR}" ] && [ -d "${TMP_DIR}" ]; then
    for log_file in outbox-worker.log bff.log vite.log; do
      if [ -f "${TMP_DIR}/${log_file}" ]; then
        echo "--- ${log_file} (failure tail) ---" >&2
        tail -120 "${TMP_DIR}/${log_file}" >&2 || true
      fi
    done
  fi
  if [ -n "${WORKER_PID}" ] && kill -0 "${WORKER_PID}" 2>/dev/null; then
    kill "${WORKER_PID}" 2>/dev/null || true
    wait "${WORKER_PID}" 2>/dev/null || true
  fi
  if [ -n "${UI_PID}" ] && kill -0 "${UI_PID}" 2>/dev/null; then
    kill "${UI_PID}" 2>/dev/null || true
    wait "${UI_PID}" 2>/dev/null || true
  fi
  if [ -n "${BFF_PID}" ] && kill -0 "${BFF_PID}" 2>/dev/null; then
    kill "${BFF_PID}" 2>/dev/null || true
    wait "${BFF_PID}" 2>/dev/null || true
  fi
  if [ -n "${DAGSTER_PID}" ] && kill -0 "${DAGSTER_PID}" 2>/dev/null; then
    kill "${DAGSTER_PID}" 2>/dev/null || true
    wait "${DAGSTER_PID}" 2>/dev/null || true
  fi
  if [ -n "${CALLBACK_PID}" ] && kill -0 "${CALLBACK_PID}" 2>/dev/null; then
    kill "${CALLBACK_PID}" 2>/dev/null || true
    wait "${CALLBACK_PID}" 2>/dev/null || true
  fi
  if [ -n "${TMP_DIR}" ]; then
    rm -rf "${TMP_DIR}"
  fi
  if [ -n "${REAL_STACK_DB_NAME}" ] && [ "${AURIS_E2E_KEEP_MYSQL_DATABASE:-0}" != "1" ]; then
    drop_real_stack_database "${REAL_STACK_DB_NAME}" || true
  fi
  exit "${status}"
}
trap cleanup EXIT INT TERM

free_port() {
  "${PYTHON_BIN}" - <<'PY'
import socket

with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
}

wait_for_url() {
  local url="$1"
  local label="$2"
  local log_file="${3:-}"
  for _ in $(seq 1 80); do
    if curl -fsS "${url}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.25
  done
  echo "${label} did not become ready: ${url}" >&2
  if [ -n "${log_file}" ] && [ -f "${log_file}" ]; then
    echo "--- ${label} log ---" >&2
    tail -80 "${log_file}" >&2 || true
  fi
  return 1
}

wait_for_worker_health() {
  local pid="$1"
  local health_path="$2"
  local log_file="$3"
  for _ in $(seq 1 160); do
    if ! kill -0 "${pid}" 2>/dev/null; then
      echo "Managed outbox worker exited before becoming healthy." >&2
      if [ -f "${log_file}" ]; then
        tail -100 "${log_file}" >&2 || true
      fi
      return 1
    fi
    if [ -f "${health_path}" ] && "${PYTHON_BIN}" - "${health_path}" "${pid}" <<'PY'
import json
import sys
from pathlib import Path

try:
    state = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except (OSError, ValueError):
    raise SystemExit(1)
expected_pid = int(sys.argv[2])
raise SystemExit(
    0
    if state.get("status") == "running"
    and state.get("healthy") is True
    and state.get("pid") == expected_pid
    else 1
)
PY
    then
      return 0
    fi
    sleep 0.1
  done
  echo "Managed outbox worker did not publish a healthy state: ${health_path}" >&2
  if [ -f "${log_file}" ]; then
    tail -100 "${log_file}" >&2 || true
  fi
  return 1
}

assert_worker_running() {
  if [ -z "${WORKER_PID}" ] || ! kill -0 "${WORKER_PID}" 2>/dev/null; then
    echo "Managed outbox worker is not running." >&2
    if [ -n "${TMP_DIR}" ] && [ -f "${TMP_DIR}/outbox-worker.log" ]; then
      tail -100 "${TMP_DIR}/outbox-worker.log" >&2 || true
    fi
    return 1
  fi
}

wait_for_real_stack() {
  local db_url="$1"
  DATABASE_URL="${db_url}" "${PYTHON_BIN}" - <<'PY'
import os
import sys
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

from redis import Redis
from sqlalchemy import create_engine, text


def http_ready(url: str, headers: dict[str, str] | None = None) -> bool:
    try:
        with urlopen(Request(url, headers=headers or {}), timeout=0.5) as response:
            return 200 <= response.status < 300
    except (OSError, URLError, ValueError):
        return False


db_url = os.environ["DATABASE_URL"]
redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
object_endpoint = os.environ.get("OBJECT_STORAGE_ENDPOINT", "http://127.0.0.1:9000")
qdrant_url = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333")
qdrant_api_key = os.environ.get("QDRANT_API_KEY", "")
last_error = "not started"
for _ in range(120):
    try:
        engine = create_engine(db_url, pool_pre_ping=True)
        with engine.connect() as connection:
            connection.execute(text("select 1"))
        Redis.from_url(redis_url, socket_connect_timeout=0.5, socket_timeout=0.5).ping()
        if not http_ready(f"{object_endpoint.rstrip('/')}/minio/health/ready"):
            raise RuntimeError("minio not ready")
        if not http_ready(
            f"{qdrant_url.rstrip('/')}/collections",
            {"api-key": qdrant_api_key} if qdrant_api_key else None,
        ):
            raise RuntimeError("qdrant not ready")
        sys.exit(0)
    except Exception as exc:
        last_error = f"{exc.__class__.__name__}: {exc}"
        time.sleep(1)
print(f"real dependency stack did not become ready: {last_error}", file=sys.stderr)
sys.exit(1)
PY
}

prepare_real_stack_database() {
  local database_name="$1"
  AURIS_E2E_DB_NAME="${database_name}" "${PYTHON_BIN}" - <<'PY'
import os
import re
import sys

from sqlalchemy import create_engine


database_name = os.environ["AURIS_E2E_DB_NAME"]
if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", database_name):
    raise SystemExit(f"unsafe MySQL database name: {database_name!r}")

admin_url = os.environ.get(
    "AURIS_E2E_MYSQL_ADMIN_URL",
    "mysql+pymysql://root:auris_root@127.0.0.1:3306/mysql",
)
engine = create_engine(admin_url, pool_pre_ping=True)
try:
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.exec_driver_sql(f"DROP DATABASE IF EXISTS `{database_name}`")
        connection.exec_driver_sql(
            f"CREATE DATABASE `{database_name}` "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
        connection.exec_driver_sql(f"GRANT ALL PRIVILEGES ON `{database_name}`.* TO 'auris'@'%%'")
finally:
    engine.dispose()

sys.stdout.write(f"mysql+pymysql://auris:auris@127.0.0.1:3306/{database_name}")
PY
}

drop_real_stack_database() {
  local database_name="$1"
  AURIS_E2E_DB_NAME="${database_name}" "${PYTHON_BIN}" - <<'PY'
import os
import re

from sqlalchemy import create_engine


database_name = os.environ["AURIS_E2E_DB_NAME"]
if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", database_name):
    raise SystemExit(f"unsafe MySQL database name: {database_name!r}")

admin_url = os.environ.get(
    "AURIS_E2E_MYSQL_ADMIN_URL",
    "mysql+pymysql://root:auris_root@127.0.0.1:3306/mysql",
)
engine = create_engine(admin_url, pool_pre_ping=True)
try:
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.exec_driver_sql(f"DROP DATABASE IF EXISTS `{database_name}`")
finally:
    engine.dispose()
PY
}

assert_strict_readyz() {
  local bff_url="$1"
  "${PYTHON_BIN}" - "${bff_url}" <<'PY'
import json
import sys
from urllib.request import urlopen

url = sys.argv[1].rstrip("/") + "/readyz"
with urlopen(url, timeout=2) as response:
    payload = json.loads(response.read().decode("utf-8"))
if payload.get("status") != "ok":
    raise SystemExit(f"strict readyz is not ok: {payload}")
checks = payload.get("data", {}).get("checks", {})
required = set(payload.get("data", {}).get("required_checks", []))
expected = {"database", "redis", "object_storage", "qdrant", "dagster"}
missing_required_names = expected - required
if missing_required_names:
    raise SystemExit(f"strict readyz missing required checks: {sorted(missing_required_names)}")
not_ok = {name: checks.get(name) for name in expected if checks.get(name) != "ok"}
if not_ok:
    raise SystemExit(f"strict readyz dependencies are not ok: {not_ok}")
PY
}

echo "Checking UI/BFF E2E target: ${E2E_URL}"
if [ "${FORCE_AUTOSTART}" = "1" ] || ! curl -fsS "${E2E_URL%/}/healthz" >/dev/null 2>&1; then
  if [ "${AUTOSTART}" != "1" ]; then
    echo "UI/BFF E2E target is not reachable and AURIS_E2E_AUTOSTART=${AUTOSTART}" >&2
    exit 1
  fi

  TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/auris-ui-bff-e2e.XXXXXX")"
  BFF_PORT="${AURIS_E2E_BFF_PORT:-$(free_port)}"
  if [ -n "${AURIS_E2E_UI_PORT:-}" ]; then
    UI_PORT="${AURIS_E2E_UI_PORT}"
  elif [ "${FORCE_AUTOSTART}" = "1" ]; then
    UI_PORT="$(free_port)"
  else
    UI_PORT="5173"
  fi
  if [ "${REAL_STACK}" = "1" ]; then
    DB_URL="${DATABASE_URL:-mysql+pymysql://auris:auris@127.0.0.1:3306/auris_flow}"
  else
    DB_PATH="${TMP_DIR}/auris_flow_e2e.sqlite"
    DB_URL="sqlite:///${DB_PATH}"
  fi
  BFF_URL="http://127.0.0.1:${BFF_PORT}"
  E2E_URL="http://127.0.0.1:${UI_PORT}/"

  if [ "${REAL_STACK}" = "1" ]; then
    echo "Autostarting BFF and UI for E2E with MySQL/Redis/MinIO/Qdrant"
    if ! command -v docker >/dev/null 2>&1; then
      echo "AURIS_REAL_STACK_E2E=1 requires Docker." >&2
      exit 2
    fi
    docker compose -f "${COMPOSE_FILE}" up -d mysql redis minio qdrant
    wait_for_real_stack "${DB_URL}"
    echo "Bootstrapping the authenticated MinIO E2E bucket..."
    run_minio_bootstrap
    if [ -z "${DATABASE_URL:-}" ] && [ "${AURIS_E2E_ISOLATED_MYSQL_DB:-1}" = "1" ]; then
      REAL_STACK_DB_NAME="auris_flow_e2e_$(date +%s)_${BFF_PORT}"
      DB_URL="$(prepare_real_stack_database "${REAL_STACK_DB_NAME}")"
    fi
    if [ -n "${AURIS_E2E_MIGRATION_DATABASE_URL:-}" ]; then
      MIGRATION_DB_URL="${AURIS_E2E_MIGRATION_DATABASE_URL}"
    elif [ -n "${REAL_STACK_DB_NAME}" ]; then
      MIGRATION_DB_URL="mysql+pymysql://root:auris_root@127.0.0.1:3306/${REAL_STACK_DB_NAME}"
    else
      MIGRATION_DB_URL="${DB_URL}"
    fi
    DAGSTER_PORT="${AURIS_E2E_DAGSTER_PORT:-$(free_port)}"
    DAGSTER_GRAPHQL_URL="${DAGSTER_GRAPHQL_URL:-http://127.0.0.1:${DAGSTER_PORT}/graphql}"
    DAGSTER_RECEIPT_LOG="${TMP_DIR}/dagster-receipts.jsonl"
    "${PYTHON_BIN}" scripts/fake_dagster_graphql_server.py \
      --host 127.0.0.1 \
      --port "${DAGSTER_PORT}" \
      --receipt-log "${DAGSTER_RECEIPT_LOG}" >"${TMP_DIR}/dagster.log" 2>&1 &
    DAGSTER_PID=$!
    wait_for_url "http://127.0.0.1:${DAGSTER_PORT}/healthz" "Dagster protocol fake" "${TMP_DIR}/dagster.log"
    CALLBACK_PORT="${AURIS_E2E_CALLBACK_PORT:-$(free_port)}"
    EXTERNAL_CALLBACK_URL="${EXTERNAL_CALLBACK_URL:-http://127.0.0.1:${CALLBACK_PORT}/callbacks/platform}"
    EXTERNAL_CALLBACK_KEY_BINDINGS="${CALLBACK_KEY_BINDINGS_JSON}"
    EXTERNAL_CALLBACK_ACTIVE_KEY_ID="${CALLBACK_HMAC_KEY_ID}"
    export EXTERNAL_CALLBACK_URL EXTERNAL_CALLBACK_KEY_BINDINGS
    export EXTERNAL_CALLBACK_ACTIVE_KEY_ID
    CALLBACK_RECEIPT_LOG="${TMP_DIR}/callback-receipts.jsonl"
    "${PYTHON_BIN}" scripts/fake_platform_callback_server.py \
      --host 127.0.0.1 \
      --port "${CALLBACK_PORT}" \
      --key-bindings "${EXTERNAL_CALLBACK_KEY_BINDINGS}" \
      --active-key-id "${EXTERNAL_CALLBACK_ACTIVE_KEY_ID}" \
      --receipt-log "${CALLBACK_RECEIPT_LOG}" >"${TMP_DIR}/callback.log" 2>&1 &
    CALLBACK_PID=$!
    wait_for_url "http://127.0.0.1:${CALLBACK_PORT}/healthz" "Platform callback fake" "${TMP_DIR}/callback.log"
  else
    echo "Autostarting BFF and UI for E2E"
    MIGRATION_DB_URL="${DB_URL}"
  fi
  echo "  BFF: ${BFF_URL}"
  echo "  UI:  ${E2E_URL}"
  echo "  DB:  ${DB_URL}"

  (
    cd backend
    # E2E runs against a fresh database and must include the active linear head.
    DATABASE_URL="${MIGRATION_DB_URL}" "${PYTHON_BIN}" -m alembic upgrade head >/dev/null
    DATABASE_URL="${DB_URL}" "${PYTHON_BIN}" -m app.seed local_demo >/dev/null
  )

  (
    cd backend
    if [ "${REAL_STACK}" = "1" ]; then
      exec env APP_ENV=ci ALLOW_DEV_AUTH=true DEPENDENCY_CHECK_MODE=strict \
        REQUIRED_DEPENDENCY_CHECKS=database,redis,object_storage,qdrant,dagster \
        "COMPLETION_RECEIPT_SECRET=${COMPLETION_HMAC_VALUE}" \
        COMPLETION_RECEIPT_KEY_BINDINGS="${COMPLETION_KEY_BINDINGS_JSON}" \
        AUDIO_PLAYBACK_GRANT_SECRET=${AUDIO_PLAYBACK_GRANT_SECRET:-${DEFAULT_PLAYBACK_GRANT_HMAC_VALUE}} \
        COMPLETION_RECEIPT_SIGNATURE_ID="${COMPLETION_HMAC_KEY_ID}" \
        AURIS_QDRANT_ADAPTER=real AURIS_OBJECT_STORAGE_ADAPTER=real AURIS_DAGSTER_ADAPTER=real \
        QDRANT_API_KEY="${QDRANT_API_KEY}" \
        AURIS_EXTERNAL_CALLBACK_ADAPTER=real \
        OBJECT_STORAGE_ENDPOINT="${OBJECT_STORAGE_ENDPOINT:-http://127.0.0.1:9000}" \
        OBJECT_STORAGE_BUCKET="${OBJECT_STORAGE_BUCKET:-auris-flow-local}" \
        OBJECT_STORAGE_ACCESS_KEY="${OBJECT_STORAGE_ACCESS_KEY:-minioadmin}" \
        OBJECT_STORAGE_SECRET_KEY="${OBJECT_STORAGE_SECRET_KEY:-minioadmin}" \
        OBJECT_STORAGE_REGION="${OBJECT_STORAGE_REGION:-us-east-1}" \
        EXTERNAL_CALLBACK_URL="${EXTERNAL_CALLBACK_URL}" \
        DAGSTER_GRAPHQL_URL="${DAGSTER_GRAPHQL_URL}" DATABASE_URL="${DB_URL}" "${PYTHON_BIN}" -m uvicorn app.main:app \
        --host 127.0.0.1 --port "${BFF_PORT}" >"${TMP_DIR}/bff.log" 2>&1
    else
      exec env APP_ENV=local ALLOW_DEV_AUTH=true \
        "COMPLETION_RECEIPT_SECRET=${COMPLETION_HMAC_VALUE}" \
        COMPLETION_RECEIPT_KEY_BINDINGS="${COMPLETION_KEY_BINDINGS_JSON}" \
        AUDIO_PLAYBACK_GRANT_SECRET=${AUDIO_PLAYBACK_GRANT_SECRET:-${DEFAULT_PLAYBACK_GRANT_HMAC_VALUE}} \
        COMPLETION_RECEIPT_SIGNATURE_ID="${COMPLETION_HMAC_KEY_ID}" \
        DATABASE_URL="${DB_URL}" "${PYTHON_BIN}" -m uvicorn app.main:app \
        --host 127.0.0.1 --port "${BFF_PORT}" >"${TMP_DIR}/bff.log" 2>&1
    fi
  ) &
  BFF_PID=$!
  wait_for_url "${BFF_URL}/healthz" "BFF" "${TMP_DIR}/bff.log"
  if [ "${REAL_STACK}" = "1" ]; then
    assert_strict_readyz "${BFF_URL}"
  fi

  WORKER_HEALTH_PATH="${TMP_DIR}/outbox-worker-health.json"
  (
    cd backend
    if [ "${REAL_STACK}" = "1" ]; then
      exec env APP_ENV=ci ALLOW_DEV_AUTH=true \
        "COMPLETION_RECEIPT_SECRET=${COMPLETION_HMAC_VALUE}" \
        COMPLETION_RECEIPT_KEY_BINDINGS="${COMPLETION_KEY_BINDINGS_JSON}" \
        COMPLETION_RECEIPT_SIGNATURE_ID="${COMPLETION_HMAC_KEY_ID}" \
        AURIS_QDRANT_ADAPTER=real AURIS_OBJECT_STORAGE_ADAPTER=real AURIS_DAGSTER_ADAPTER=real \
        QDRANT_API_KEY="${QDRANT_API_KEY}" \
        AURIS_EXTERNAL_CALLBACK_ADAPTER=real \
        OBJECT_STORAGE_ENDPOINT="${OBJECT_STORAGE_ENDPOINT:-http://127.0.0.1:9000}" \
        OBJECT_STORAGE_BUCKET="${OBJECT_STORAGE_BUCKET:-auris-flow-local}" \
        OBJECT_STORAGE_ACCESS_KEY="${OBJECT_STORAGE_ACCESS_KEY:-minioadmin}" \
        OBJECT_STORAGE_SECRET_KEY="${OBJECT_STORAGE_SECRET_KEY:-minioadmin}" \
        OBJECT_STORAGE_REGION="${OBJECT_STORAGE_REGION:-us-east-1}" \
        EXTERNAL_CALLBACK_URL="${EXTERNAL_CALLBACK_URL}" \
        DAGSTER_GRAPHQL_URL="${DAGSTER_GRAPHQL_URL}" \
        DATABASE_URL="${DB_URL}" \
        AURIS_OUTBOX_HEALTH_PATH="${WORKER_HEALTH_PATH}" \
        AURIS_OUTBOX_POLL_INTERVAL_SECONDS="${AURIS_E2E_WORKER_POLL_INTERVAL_SECONDS:-0.05}" \
        AURIS_OUTBOX_MAX_IDLE_WAIT_SECONDS="${AURIS_E2E_WORKER_MAX_IDLE_WAIT_SECONDS:-0.5}" \
        AURIS_OUTBOX_HEARTBEAT_INTERVAL_SECONDS="${AURIS_E2E_WORKER_HEARTBEAT_INTERVAL_SECONDS:-1}" \
        "${PYTHON_BIN}" -m app.workers.outbox_worker
    else
      exec env APP_ENV=local ALLOW_DEV_AUTH=true \
        "COMPLETION_RECEIPT_SECRET=${COMPLETION_HMAC_VALUE}" \
        COMPLETION_RECEIPT_KEY_BINDINGS="${COMPLETION_KEY_BINDINGS_JSON}" \
        COMPLETION_RECEIPT_SIGNATURE_ID="${COMPLETION_HMAC_KEY_ID}" \
        AURIS_QDRANT_ADAPTER=local AURIS_OBJECT_STORAGE_ADAPTER=local AURIS_DAGSTER_ADAPTER=local \
        AURIS_EXTERNAL_CALLBACK_ADAPTER=local \
        DATABASE_URL="${DB_URL}" \
        AURIS_OUTBOX_HEALTH_PATH="${WORKER_HEALTH_PATH}" \
        AURIS_OUTBOX_POLL_INTERVAL_SECONDS="${AURIS_E2E_WORKER_POLL_INTERVAL_SECONDS:-0.05}" \
        AURIS_OUTBOX_MAX_IDLE_WAIT_SECONDS="${AURIS_E2E_WORKER_MAX_IDLE_WAIT_SECONDS:-0.5}" \
        AURIS_OUTBOX_HEARTBEAT_INTERVAL_SECONDS="${AURIS_E2E_WORKER_HEARTBEAT_INTERVAL_SECONDS:-1}" \
        "${PYTHON_BIN}" -m app.workers.outbox_worker
    fi
  ) >"${TMP_DIR}/outbox-worker.log" 2>&1 &
  WORKER_PID=$!
  wait_for_worker_health "${WORKER_PID}" "${WORKER_HEALTH_PATH}" "${TMP_DIR}/outbox-worker.log"
  if [ "${REAL_STACK}" = "1" ]; then
    DATABASE_URL="${DB_URL}" AURIS_E2E_BFF_URL="${BFF_URL}" \
      AURIS_E2E_WORKER_HEALTH_PATH="${WORKER_HEALTH_PATH}" \
      AURIS_OBJECT_STORAGE_ADAPTER=real \
      OBJECT_STORAGE_ENDPOINT="${OBJECT_STORAGE_ENDPOINT:-http://127.0.0.1:9000}" \
      OBJECT_STORAGE_BUCKET="${OBJECT_STORAGE_BUCKET:-auris-flow-local}" \
      OBJECT_STORAGE_ACCESS_KEY="${OBJECT_STORAGE_ACCESS_KEY:-minioadmin}" \
      OBJECT_STORAGE_SECRET_KEY="${OBJECT_STORAGE_SECRET_KEY:-minioadmin}" \
      OBJECT_STORAGE_REGION="${OBJECT_STORAGE_REGION:-us-east-1}" \
      "${PYTHON_BIN}" scripts/seed_real_audio_fixture.py
    DATABASE_URL="${DB_URL}" AURIS_E2E_BFF_URL="${BFF_URL}" \
      AURIS_E2E_WORKER_HEALTH_PATH="${WORKER_HEALTH_PATH}" \
      AURIS_E2E_SEED_REAL_AUDIO_FIXTURE=1 \
      AURIS_OBJECT_STORAGE_ADAPTER=real \
      OBJECT_STORAGE_ENDPOINT="${OBJECT_STORAGE_ENDPOINT:-http://127.0.0.1:9000}" \
      OBJECT_STORAGE_BUCKET="${OBJECT_STORAGE_BUCKET:-auris-flow-local}" \
      OBJECT_STORAGE_ACCESS_KEY="${OBJECT_STORAGE_ACCESS_KEY:-minioadmin}" \
      OBJECT_STORAGE_SECRET_KEY="${OBJECT_STORAGE_SECRET_KEY:-minioadmin}" \
      OBJECT_STORAGE_REGION="${OBJECT_STORAGE_REGION:-us-east-1}" \
      "${PYTHON_BIN}" scripts/verify_e2e_outbox_dispatch.py
  fi

  (
    cd prototype/auris-flow-ui
    exec env VITE_API_PROXY_TARGET="${BFF_URL}" ./node_modules/.bin/vite \
      --host 127.0.0.1 --port "${UI_PORT}" --strictPort >"${TMP_DIR}/vite.log" 2>&1
  ) &
  UI_PID=$!
  wait_for_url "${E2E_URL}" "UI" "${TMP_DIR}/vite.log"
  wait_for_url "${E2E_URL%/}/healthz" "UI proxy" "${TMP_DIR}/vite.log"
fi

if [ "${REAL_STACK}" = "1" ] && [ -z "${BFF_URL:-}" ]; then
  echo "AURIS_REAL_STACK_E2E=1 with an already-running UI requires autostart or a script-managed BFF." >&2
  echo "Stop the UI and rerun, or unset AURIS_REAL_STACK_E2E for the existing SQLite/mock target." >&2
  exit 2
fi

env AURIS_E2E_URL="${E2E_URL}" \
  AURIS_E2E_BFF_URL="${BFF_URL:-${E2E_URL}}" \
  AURIS_REAL_STACK_E2E="${REAL_STACK}" \
  AURIS_E2E_RUN_ID="${E2E_RUN_ID}" \
  AURIS_E2E_RESULT_PATH="${RESULT_PATH}" \
  AURIS_E2E_COMPLETION_HMAC_KEY_ID="${COMPLETION_HMAC_KEY_ID}" \
  "AURIS_E2E_COMPLETION_HMAC_SECRET=${COMPLETION_HMAC_VALUE}" \
  OBJECT_STORAGE_PROVIDER="${OBJECT_STORAGE_PROVIDER:-minio}" \
  OBJECT_STORAGE_ENDPOINT="${OBJECT_STORAGE_ENDPOINT:-http://127.0.0.1:9000}" \
  OBJECT_STORAGE_BUCKET="${OBJECT_STORAGE_BUCKET:-auris-flow-local}" \
  OBJECT_STORAGE_ACCESS_KEY="${OBJECT_STORAGE_ACCESS_KEY:-minioadmin}" \
  OBJECT_STORAGE_SECRET_KEY="${OBJECT_STORAGE_SECRET_KEY:-minioadmin}" \
  OBJECT_STORAGE_REGION="${OBJECT_STORAGE_REGION:-us-east-1}" \
  AURIS_E2E_ASYNC_DISPATCH_TIMEOUT_MS="${AURIS_E2E_ASYNC_DISPATCH_TIMEOUT_MS:-60000}" \
  AURIS_E2E_ASYNC_DISPATCH_POLL_MS="${AURIS_E2E_ASYNC_DISPATCH_POLL_MS:-100}" \
  npm --prefix prototype/auris-flow-ui run e2e:bff
if [ -n "${DB_URL:-}" ]; then
  assert_worker_running
fi
AURIS_E2E_RUN_ID="${E2E_RUN_ID}" AURIS_REAL_STACK_E2E="${REAL_STACK}" \
  npm --prefix prototype/auris-flow-ui run e2e:bff:check -- "${RESULT_PATH}"
if [ -n "${DB_URL:-}" ]; then
  if [ "${REAL_STACK}" = "1" ]; then
    env DATABASE_URL="${DB_URL}" AURIS_E2E_RUN_ID="${E2E_RUN_ID}" AURIS_E2E_RESULT_PATH="${RESULT_PATH}" AURIS_E2E_OUTBOX_RESULT_PATH="${OUTBOX_RESULT_PATH}" AURIS_E2E_BFF_URL="${BFF_URL}" \
      AURIS_E2E_WORKER_HEALTH_PATH="${WORKER_HEALTH_PATH}" \
      "COMPLETION_RECEIPT_SECRET=${COMPLETION_HMAC_VALUE}" \
      COMPLETION_RECEIPT_KEY_BINDINGS="${COMPLETION_KEY_BINDINGS_JSON}" \
      COMPLETION_RECEIPT_SIGNATURE_ID="${COMPLETION_HMAC_KEY_ID}" \
      AURIS_QDRANT_ADAPTER=real AURIS_OBJECT_STORAGE_ADAPTER=real AURIS_DAGSTER_ADAPTER=real \
      QDRANT_API_KEY="${QDRANT_API_KEY}" \
      AURIS_EXTERNAL_CALLBACK_ADAPTER=real \
      OBJECT_STORAGE_ENDPOINT="${OBJECT_STORAGE_ENDPOINT:-http://127.0.0.1:9000}" \
      OBJECT_STORAGE_BUCKET="${OBJECT_STORAGE_BUCKET:-auris-flow-local}" \
      OBJECT_STORAGE_ACCESS_KEY="${OBJECT_STORAGE_ACCESS_KEY:-minioadmin}" \
      OBJECT_STORAGE_SECRET_KEY="${OBJECT_STORAGE_SECRET_KEY:-minioadmin}" \
      OBJECT_STORAGE_REGION="${OBJECT_STORAGE_REGION:-us-east-1}" \
      EXTERNAL_CALLBACK_URL="${EXTERNAL_CALLBACK_URL}" \
      DAGSTER_GRAPHQL_URL="${DAGSTER_GRAPHQL_URL}" \
      AURIS_E2E_DAGSTER_RECEIPT_LOG="${DAGSTER_RECEIPT_LOG}" \
      AURIS_E2E_CALLBACK_RECEIPT_LOG="${CALLBACK_RECEIPT_LOG}" \
      "${PYTHON_BIN}" scripts/verify_e2e_outbox_dispatch.py
  else
    DATABASE_URL="${DB_URL}" AURIS_E2E_RUN_ID="${E2E_RUN_ID}" AURIS_E2E_RESULT_PATH="${RESULT_PATH}" AURIS_E2E_OUTBOX_RESULT_PATH="${OUTBOX_RESULT_PATH}" AURIS_E2E_BFF_URL="${BFF_URL}" \
      AURIS_E2E_WORKER_HEALTH_PATH="${WORKER_HEALTH_PATH}" \
      COMPLETION_RECEIPT_SECRET=${COMPLETION_RECEIPT_SECRET:-${DEFAULT_COMPLETION_HMAC_VALUE}} \
      COMPLETION_RECEIPT_SIGNATURE_ID="${COMPLETION_RECEIPT_SIGNATURE_ID:-auris-e2e-completion}" \
      "${PYTHON_BIN}" scripts/verify_e2e_outbox_dispatch.py
  fi
  AURIS_E2E_RUN_ID="${E2E_RUN_ID}" AURIS_REAL_STACK_E2E="${REAL_STACK}" \
    npm --prefix prototype/auris-flow-ui run e2e:bff:check -- \
      "${RESULT_PATH}" "${OUTBOX_RESULT_PATH}"
else
  echo "Skipping E2E outbox dispatch verification because this run is using an externally managed target." >&2
  if [ "${AURIS_REQUIRE_E2E_OUTBOX:-0}" = "1" ]; then
    echo "AURIS_REQUIRE_E2E_OUTBOX=1 requires script-managed UI/BFF autostart." >&2
    exit 2
  fi
fi
