#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -n "${PYTHON:-}" ]; then
  PYTHON_BIN="${PYTHON}"
elif [ -x "${ROOT}/backend/.venv/bin/python" ]; then
  PYTHON_BIN="${ROOT}/backend/.venv/bin/python"
else
  PYTHON_BIN="$(command -v python3)"
fi
COMPOSE_FILE="${ROOT}/docker/local/docker-compose.yml"
export QDRANT_API_KEY=${QDRANT_API_KEY:-auris-qdrant-test-key}
ARTIFACT_DIR="${ROOT}/prototype/auris-flow-ui/e2e/artifacts"
ARTIFACT_SUFFIX="${AURIS_REAL_STACK_ARTIFACT_SUFFIX:-$(date +%s)-$$}"
PLATFORM_RESULT="${AURIS_REAL_STACK_PLATFORM_RESULT:-${ARTIFACT_DIR}/platform-bff-result-${ARTIFACT_SUFFIX}.json}"
OUTBOX_RESULT="${AURIS_REAL_STACK_OUTBOX_RESULT:-${ARTIFACT_DIR}/outbox-dispatch-result-${ARTIFACT_SUFFIX}.json}"
VERIFICATION_RESULT="${AURIS_REAL_STACK_VERIFICATION_RESULT:-${ROOT}/build/release-evidence/real-stack-gate.json}"
SOURCE_COMMIT="$(git -C "${ROOT}" rev-parse --verify HEAD^{commit})"
DATABASE_NAME="auris_flow_e2e_$(date +%s)_$$"
DATABASE_USER="auris_e2e_$$"
DATABASE_PASSWORD="auris_e2e"
MIGRATION_DATABASE_USER="auris_migrate_$$"
MIGRATION_DATABASE_PASSWORD="auris_migrate"
DATABASE_CREATED=0
RUN_STARTED_AT="$(date +%s)"
COMPOSE=(docker compose -f "${COMPOSE_FILE}")
SERVICES=(mysql redis minio qdrant)
MINIO_BOOTSTRAP_TIMEOUT_SECONDS="${AURIS_MINIO_BOOTSTRAP_TIMEOUT_SECONDS:-60}"
COMPOSE_WAIT_TIMEOUT_SECONDS="${AURIS_REAL_STACK_WAIT_TIMEOUT:-180}"
COMPOSE_DEADLINE_GRACE="${AURIS_REAL_STACK_DEADLINE_GRACE:-15}"
unset DATABASE_URL_FILE

run_with_deadline() {
  local timeout_seconds="$1"
  local label="$2"
  shift 2
  "${PYTHON_BIN}" "${ROOT}/scripts/run_with_deadline.py" \
    --timeout-seconds "${timeout_seconds}" \
    --label "${label}" -- \
    "$@"
}

compose_with_deadline() {
  local timeout_seconds="$1"
  local label="$2"
  shift 2
  "${PYTHON_BIN}" "${ROOT}/scripts/run_with_deadline.py" \
    --timeout-seconds "${timeout_seconds}" \
    --label "${label}" -- \
    "${COMPOSE[@]}" "$@"
}

run_minio_bootstrap() {
  local container_name status
  container_name="auris-flow-minio-bootstrap-$(date +%s)-$$"
  if "${PYTHON_BIN}" "${ROOT}/scripts/run_with_deadline.py" \
    --timeout-seconds "${MINIO_BOOTSTRAP_TIMEOUT_SECONDS}" \
    --label "MinIO bootstrap" -- \
    "${COMPOSE[@]}" run --no-TTY --name "${container_name}" --rm --no-deps minio-bootstrap; then
    return 0
  else
    status=$?
  fi
  if [ "${status}" -eq 124 ]; then
    docker rm --force "${container_name}" >/dev/null 2>&1 || true
  fi
  return "${status}"
}

mysql_exec() {
  compose_with_deadline 30 "real-stack MySQL command" \
    exec -T -e MYSQL_PWD=auris_root mysql \
    mysql --protocol=socket --user=root --connect-timeout=5 \
    --batch --skip-column-names "$@"
}

cleanup() {
  local status="$1"
  trap - EXIT INT TERM
  if [ "${DATABASE_CREATED}" = "1" ]; then
    if ! mysql_exec --execute "DROP DATABASE IF EXISTS \`${DATABASE_NAME}\`; DROP USER IF EXISTS '${DATABASE_USER}'@'%'; DROP USER IF EXISTS '${MIGRATION_DATABASE_USER}'@'%';" >/dev/null 2>&1; then
      echo "Could not drop temporary MySQL database/users ${DATABASE_NAME}/${DATABASE_USER}/${MIGRATION_DATABASE_USER}." >&2
      if [ "${status}" -eq 0 ]; then
        status=1
      fi
    fi
  fi
  exit "${status}"
}
trap 'cleanup "$?"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

assert_compose_health() {
  local service container_id health
  for service in "${SERVICES[@]}"; do
    container_id="$("${COMPOSE[@]}" ps --quiet "${service}")"
    if [ -z "${container_id}" ]; then
      echo "Compose service is not running: ${service}" >&2
      return 1
    fi
    health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "${container_id}")"
    if [ "${health}" != "healthy" ]; then
      echo "Compose service is not healthy: ${service} (${health})" >&2
      "${COMPOSE[@]}" logs --tail 80 "${service}" >&2 || true
      return 1
    fi
    echo "  ${service}: healthy"
  done
}

verify_qdrant_api_key_gate() {
  QDRANT_URL="${QDRANT_URL:-http://127.0.0.1:6333}" \
    QDRANT_API_KEY="${QDRANT_API_KEY}" "${PYTHON_BIN}" - <<'PY'
import json
import os
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

base_url = os.environ["QDRANT_URL"].rstrip("/")
api_key = os.environ["QDRANT_API_KEY"]
collection = f"auris_release_gate_{int(time.time())}_{os.getpid()}"


def request(method: str, path: str, body=None, *, authenticated: bool = True):
    headers = {"Content-Type": "application/json"}
    if authenticated:
        headers["api-key"] = api_key
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = Request(f"{base_url}{path}", data=data, method=method, headers=headers)
    with urlopen(req, timeout=5) as response:
        raw = response.read().decode("utf-8")
        return response.status, json.loads(raw) if raw else {}


try:
    request("GET", "/collections", authenticated=False)
except HTTPError as exc:
    if exc.code not in {401, 403}:
        raise SystemExit(f"anonymous Qdrant request returned {exc.code}, expected 401/403")
    anonymous_status = exc.code
else:
    raise SystemExit("anonymous Qdrant request unexpectedly succeeded")

try:
    status, _ = request(
        "PUT",
        f"/collections/{collection}",
        {"vectors": {"size": 4, "distance": "Cosine"}},
    )
    if status not in {200, 201}:
        raise SystemExit(f"authenticated collection create failed: HTTP {status}")
    status, upsert = request(
        "PUT",
        f"/collections/{collection}/points?wait=true",
        {"points": [{"id": 1, "vector": [1.0, 0.0, 0.0, 0.0], "payload": {"gate": "p1"}}]},
    )
    if status != 200 or upsert.get("status") != "ok":
        raise SystemExit(f"authenticated Qdrant upsert failed: {upsert}")
    status, search = request(
        "POST",
        f"/collections/{collection}/points/search",
        {"vector": [1.0, 0.0, 0.0, 0.0], "limit": 1, "with_payload": True},
    )
    points = search.get("result", [])
    if status != 200 or not points or points[0].get("id") != 1:
        raise SystemExit(f"authenticated Qdrant search failed: {search}")
finally:
    try:
        request("DELETE", f"/collections/{collection}")
    except Exception:
        pass

print(
    "Qdrant API-key gate ok: "
    f"anonymous_http={anonymous_status}, authenticated_upsert=ok, authenticated_search=ok"
)
PY
}

verify_migration_mysql_privilege_boundary() {
  local database_url="$1"
  shift
  run_with_deadline 60 "real-stack migration MySQL security check" \
    env -u DATABASE_URL_FILE DATABASE_URL="${database_url}" \
    "${PYTHON_BIN}" "${ROOT}/backend/scripts/verify_mysql_migration_security.py" \
    --expected-database "${DATABASE_NAME}" \
    --expected-user "${MIGRATION_DATABASE_USER}" \
    --expected-version-prefix 8.4. \
    --privilege-profile migration \
    "$@"
}

verify_runtime_mysql_exact_grants() {
  local database_url="$1"
  run_with_deadline 60 "real-stack runtime MySQL security check" \
    env -u DATABASE_URL_FILE DATABASE_URL="${database_url}" \
    "${PYTHON_BIN}" "${ROOT}/backend/scripts/verify_mysql_migration_security.py" \
    --expected-database "${DATABASE_NAME}" \
    --expected-user "${DATABASE_USER}" \
    --expected-version-prefix 8.4. \
    --privilege-profile runtime \
    --require-runtime-trigger-probe
}

if [ "${AURIS_SKIP_REAL_STACK_E2E:-0}" = "1" ]; then
  echo "AURIS_SKIP_REAL_STACK_E2E=1 is not allowed by scripts/verify_real_stack.sh." >&2
  exit 2
fi
if ! [[ "${COMPOSE_WAIT_TIMEOUT_SECONDS}" =~ ^[1-9][0-9]*$ && \
  "${COMPOSE_DEADLINE_GRACE}" =~ ^[1-9][0-9]*$ ]]; then
  echo "Real-stack Compose timeout values must be positive integers." >&2
  exit 2
fi
COMPOSE_WAIT_DEADLINE=$((COMPOSE_WAIT_TIMEOUT_SECONDS + COMPOSE_DEADLINE_GRACE))
if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required for real-stack verification." >&2
  exit 2
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker Engine is not available for real-stack verification." >&2
  exit 2
fi

"${COMPOSE[@]}" config --quiet
mkdir -p "${ARTIFACT_DIR}" "$(dirname "${VERIFICATION_RESULT}")"
rm -f "${PLATFORM_RESULT}" "${OUTBOX_RESULT}" "${VERIFICATION_RESULT}"

echo "Starting MySQL/Redis/MinIO/Qdrant and waiting for health checks..."
compose_with_deadline "${COMPOSE_WAIT_DEADLINE}" "start real stack" \
  up --detach --wait --wait-timeout "${COMPOSE_WAIT_TIMEOUT_SECONDS}" "${SERVICES[@]}"
assert_compose_health
echo "Bootstrapping the authenticated MinIO release-test bucket..."
run_minio_bootstrap
verify_qdrant_api_key_gate

DATABASE_CREATED=1
mysql_exec --execute "DROP DATABASE IF EXISTS \`${DATABASE_NAME}\`; DROP USER IF EXISTS '${DATABASE_USER}'@'%'; DROP USER IF EXISTS '${MIGRATION_DATABASE_USER}'@'%'; CREATE DATABASE \`${DATABASE_NAME}\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; CREATE USER '${DATABASE_USER}'@'%' IDENTIFIED BY '${DATABASE_PASSWORD}'; CREATE USER '${MIGRATION_DATABASE_USER}'@'%' IDENTIFIED BY '${MIGRATION_DATABASE_PASSWORD}'; GRANT SELECT, INSERT, UPDATE, DELETE ON \`${DATABASE_NAME}\`.* TO '${DATABASE_USER}'@'%'; GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, DROP, INDEX, REFERENCES, TRIGGER ON \`${DATABASE_NAME}\`.* TO '${MIGRATION_DATABASE_USER}'@'%'; FLUSH PRIVILEGES;" >/dev/null
DATABASE_URL="mysql+pymysql://${DATABASE_USER}:${DATABASE_PASSWORD}@127.0.0.1:3306/${DATABASE_NAME}"
MIGRATION_DATABASE_URL="mysql+pymysql://${MIGRATION_DATABASE_USER}:${MIGRATION_DATABASE_PASSWORD}@127.0.0.1:3306/${DATABASE_NAME}"

verify_migration_mysql_privilege_boundary "${MIGRATION_DATABASE_URL}"

echo "Verifying MySQL migration upgrade, legacy backfill and downgrade..."
run_with_deadline 900 "real-stack MySQL full migration cycle" \
  env -u DATABASE_URL_FILE DATABASE_URL="${MIGRATION_DATABASE_URL}" \
  "${PYTHON_BIN}" "${ROOT}/backend/scripts/verify_migrations.py" \
  --database-url "${MIGRATION_DATABASE_URL}"

echo "Running UI/BFF E2E against the isolated real stack..."
env \
  AURIS_SKIP_REAL_STACK_E2E=0 \
  AURIS_REAL_STACK_E2E=1 \
  AURIS_E2E_REAL_STACK=1 \
  AURIS_E2E_AUTOSTART=1 \
  AURIS_E2E_FORCE_AUTOSTART=1 \
  AURIS_E2E_RESULT_PATH="${PLATFORM_RESULT}" \
  AURIS_E2E_OUTBOX_RESULT_PATH="${OUTBOX_RESULT}" \
  AURIS_E2E_URL= \
  AURIS_E2E_BFF_PORT= \
  AURIS_E2E_UI_PORT= \
  AURIS_E2E_DAGSTER_PORT= \
  AURIS_E2E_CALLBACK_PORT= \
  AURIS_E2E_ISOLATED_MYSQL_DB=0 \
  AURIS_E2E_MIGRATION_DATABASE_URL="${MIGRATION_DATABASE_URL}" \
  AURIS_REQUIRE_E2E_OUTBOX=1 \
  AURIS_E2E_VERIFY_COMPLETION_RECEIPTS=1 \
  AURIS_E2E_REQUIRE_COMPLETION_RECEIPTS=1 \
  AURIS_E2E_REQUIRE_QDRANT_RECALL=1 \
  AURIS_E2E_REQUIRED_COMPLETION_ADAPTERS=dagster,object_storage,external_callback \
  AURIS_E2E_REQUIRED_DISPATCH_ADAPTERS=dagster,object_storage,external_callback,qdrant \
  AURIS_E2E_REQUIRED_DISPATCH_RUN_TYPES=knowledge_sync,knowledge_build,audio_intelligence \
  DATABASE_URL="${DATABASE_URL}" \
  REDIS_URL=redis://127.0.0.1:6379/0 \
  QDRANT_URL=http://127.0.0.1:6333 \
  QDRANT_API_KEY="${QDRANT_API_KEY}" \
  OBJECT_STORAGE_ENDPOINT=http://127.0.0.1:9000 \
  OBJECT_STORAGE_BUCKET=auris-flow-local \
  OBJECT_STORAGE_ACCESS_KEY=minioadmin \
  OBJECT_STORAGE_SECRET_KEY=minioadmin \
  OBJECT_STORAGE_REGION=us-east-1 \
  DAGSTER_GRAPHQL_URL= \
  EXTERNAL_CALLBACK_URL= \
  bash "${ROOT}/scripts/verify_ui_bff_e2e.sh"

echo "Verifying MySQL migration head, exact grants and controlled trigger definers..."
verify_migration_mysql_privilege_boundary \
  "${MIGRATION_DATABASE_URL}" --require-head-triggers

echo "Verifying MySQL runtime least-privilege and append-only enforcement..."
verify_runtime_mysql_exact_grants "${DATABASE_URL}"

echo "Rechecking dependency health after E2E..."
assert_compose_health

echo "Verifying MySQL outbox claim exclusivity and fencing..."
env DATABASE_URL="${DATABASE_URL}" APP_ENV=test ALLOW_DEV_AUTH=true \
  "${PYTHON_BIN}" "${ROOT}/scripts/verify_outbox_concurrency.py"

MYSQL_RUN_COUNT="$(mysql_exec --database="${DATABASE_NAME}" --execute "SELECT COUNT(*) FROM run_records;")"
if ! [[ "${MYSQL_RUN_COUNT}" =~ ^[1-9][0-9]*$ ]]; then
  echo "Real-stack MySQL proof is invalid: run_records=${MYSQL_RUN_COUNT}" >&2
  exit 1
fi
MYSQL_STORAGE_OBJECT_COUNT="$(mysql_exec --database="${DATABASE_NAME}" --execute "SELECT COUNT(*) FROM storage_objects WHERE status = 'registered';")"
if ! [[ "${MYSQL_STORAGE_OBJECT_COUNT}" =~ ^[1-9][0-9]*$ ]]; then
  echo "Real-stack MySQL storage metadata proof is invalid: storage_objects=${MYSQL_STORAGE_OBJECT_COUNT}" >&2
  exit 1
fi

echo "Hard-validating real-stack artifacts..."
AURIS_REAL_STACK_PLATFORM_RESULT="${PLATFORM_RESULT}" \
  AURIS_REAL_STACK_OUTBOX_RESULT="${OUTBOX_RESULT}" \
  AURIS_REAL_STACK_VERIFICATION_RESULT="${VERIFICATION_RESULT}" \
  AURIS_REAL_STACK_EXPECTED_DATABASE_REF="127.0.0.1:3306/${DATABASE_NAME}" \
  AURIS_REAL_STACK_MYSQL_RUN_COUNT="${MYSQL_RUN_COUNT}" \
  AURIS_REAL_STACK_MYSQL_STORAGE_OBJECT_COUNT="${MYSQL_STORAGE_OBJECT_COUNT}" \
  AURIS_REAL_STACK_STARTED_AT="${RUN_STARTED_AT}" \
  AURIS_REAL_STACK_SOURCE_COMMIT="${SOURCE_COMMIT}" \
  bash "${ROOT}/scripts/check_real_stack_artifact.sh"

echo "verify_real_stack ok: ${VERIFICATION_RESULT}"
