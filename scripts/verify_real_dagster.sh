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

BASE_COMPOSE="${ROOT}/production/compose.yaml"
GATE_COMPOSE="${ROOT}/production/tests/dagster-gate.compose.yaml"
RUN_SUFFIX="$(date +%s)-$$"
PROJECT_NAME="auris-dagster-gate-${RUN_SUFFIX}"
mkdir -p "${ROOT}/build/tmp"
TEMP_BASE="$(cd "${ROOT}/build/tmp" && pwd -P)"
TEMP_ROOT="$(mktemp -d "${TEMP_BASE}/auris-dagster-gate.XXXXXX")"
INITIAL_ARTIFACT="${TEMP_ROOT}/initial.json"
COMPOSE_MODEL="${TEMP_ROOT}/compose-model.json"
RESULT_ARTIFACT="${AURIS_REAL_DAGSTER_RESULT:-${ROOT}/build/release-evidence/real-dagster-gate.json}"
WAIT_TIMEOUT="${AURIS_REAL_DAGSTER_WAIT_TIMEOUT:-240}"
RUN_TIMEOUT="${AURIS_REAL_DAGSTER_RUN_TIMEOUT:-90}"
BUILD_TIMEOUT="${AURIS_REAL_DAGSTER_BUILD_TIMEOUT:-900}"
CLEANUP_TIMEOUT="${AURIS_REAL_DAGSTER_CLEANUP_TIMEOUT:-60}"
COMPOSE_DEADLINE_GRACE="${AURIS_REAL_DAGSTER_DEADLINE_GRACE:-15}"
DEADLINE_RUNNER="${ROOT}/scripts/run_with_deadline.py"
SOURCE_COMMIT="$(git -C "${ROOT}" rev-parse --verify HEAD^{commit})"

free_port() {
  "${PYTHON_BIN}" - <<'PY'
import socket

with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
}

export AURIS_DAGSTER_GATE_PORT="${AURIS_DAGSTER_GATE_PORT:-$(free_port)}"
export AURIS_DAGSTER_GATE_CALLBACK_PORT="${AURIS_DAGSTER_GATE_CALLBACK_PORT:-$(free_port)}"
export APP_ENV="ci"
export AURIS_SECRETS_DIR="${TEMP_ROOT}/secrets"
export AURIS_RUNTIME_METRICS_DIR="${TEMP_ROOT}/runtime-metrics"
export AURIS_PUBLIC_HOST="auris-dagster-gate.invalid"
export AURIS_EXTERNAL_CALLBACK_URL="https://callback.invalid/callbacks/auris-flow"
export AURIS_EXTERNAL_CALLBACK_HOST="callback.invalid"
export AURIS_EMBEDDING_ENDPOINT="https://embedding.invalid/v1/embeddings"
export AURIS_EMBEDDING_MODEL="dagster-gate-unused"
export AURIS_EMBEDDING_DIMENSION="8"
export AURIS_DAGSTER_IMAGE="auris-flow-dagster-gate:${RUN_SUFFIX}"

COMPOSE=(
  docker compose
  --parallel=1
  --project-name "${PROJECT_NAME}"
  --project-directory "${ROOT}/production"
  --file "${BASE_COMPOSE}"
  --file "${GATE_COMPOSE}"
)

compose_with_deadline() {
  local timeout_seconds="$1"
  local label="$2"
  shift 2
  "${PYTHON_BIN}" "${DEADLINE_RUNNER}" \
    --timeout-seconds "${timeout_seconds}" \
    --label "${label}" -- \
    "${COMPOSE[@]}" "$@"
}

cleanup() {
  local status="$?"
  trap - EXIT INT TERM
  if [ "${status}" -ne 0 ]; then
    compose_with_deadline "${CLEANUP_TIMEOUT}" "collect real Dagster logs" \
      logs --tail 120 \
      dagster-gate-callback dagster-code dagster-webserver dagster-daemon >&2 || true
  fi
  if [[ "${PROJECT_NAME}" =~ ^auris-dagster-gate-[0-9]+-[0-9]+$ ]]; then
    if ! compose_with_deadline "${CLEANUP_TIMEOUT}" "clean real Dagster project" \
      down --volumes --remove-orphans --timeout 20 >/dev/null 2>&1; then
      echo "Could not clean isolated real Dagster project ${PROJECT_NAME}." >&2
      if [ "${status}" -eq 0 ]; then
        status=1
      fi
    fi
  else
    echo "Refusing to clean unexpected Compose project: ${PROJECT_NAME}" >&2
    status=1
  fi
  if [[ "${TEMP_ROOT}" == "${TEMP_BASE}"/auris-dagster-gate.* ]] && [ -d "${TEMP_ROOT}" ]; then
    rm -rf -- "${TEMP_ROOT}"
  else
    echo "Refusing to remove unexpected temporary directory: ${TEMP_ROOT}" >&2
    status=1
  fi
  exit "${status}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [ "${AURIS_SKIP_REAL_DAGSTER:-0}" = "1" ]; then
  echo "AURIS_SKIP_REAL_DAGSTER=1 is not allowed by the real Dagster gate." >&2
  exit 2
fi
if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  echo "Docker Engine is required for real Dagster verification." >&2
  exit 2
fi
if ! [[ "${WAIT_TIMEOUT}" =~ ^[1-9][0-9]*$ && \
  "${RUN_TIMEOUT}" =~ ^[1-9][0-9]*$ && \
  "${BUILD_TIMEOUT}" =~ ^[1-9][0-9]*$ && \
  "${CLEANUP_TIMEOUT}" =~ ^[1-9][0-9]*$ && \
  "${COMPOSE_DEADLINE_GRACE}" =~ ^[1-9][0-9]*$ ]]; then
  echo "Real Dagster timeout values must be positive integers." >&2
  exit 2
fi
COMPOSE_WAIT_DEADLINE=$((WAIT_TIMEOUT + COMPOSE_DEADLINE_GRACE))
RUN_COMMAND_DEADLINE=$((RUN_TIMEOUT + COMPOSE_DEADLINE_GRACE))

AURIS_SECRETS_DIR="${AURIS_SECRETS_DIR}" \
  AURIS_RUNTIME_METRICS_DIR="${AURIS_RUNTIME_METRICS_DIR}" \
  bash "${ROOT}/production/scripts/init-secrets.sh" >/dev/null

compose_with_deadline "${CLEANUP_TIMEOUT}" "validate real Dagster Compose" config --quiet
compose_with_deadline "${CLEANUP_TIMEOUT}" "render real Dagster Compose" config --format json >"${COMPOSE_MODEL}"
"${PYTHON_BIN}" "${DEADLINE_RUNNER}" \
  --timeout-seconds "${CLEANUP_TIMEOUT}" \
  --label "validate real Dagster host network" -- \
  "${PYTHON_BIN}" "${ROOT}/scripts/verify_dagster_gate_network.py" \
  --compose-model "${COMPOSE_MODEL}" \
  --project-name "${PROJECT_NAME}" \
  --webserver-port "${AURIS_DAGSTER_GATE_PORT}" \
  --callback-port "${AURIS_DAGSTER_GATE_CALLBACK_PORT}" >/dev/null
mkdir -p "$(dirname "${RESULT_ARTIFACT}")"
rm -f "${RESULT_ARTIFACT}"

echo "Starting isolated production Dagster services (${PROJECT_NAME})..."
compose_with_deadline "${BUILD_TIMEOUT}" "build real Dagster gate images" \
  build dagster-code dagster-gate-callback
START_ORDER=(
  dagster-gate-secrets-init
  mysql
  dagster-gate-callback
  dagster-gate-db-bootstrap
  dagster-code
  dagster-webserver
  dagster-daemon
)
ONE_SHOT_SERVICES=(
  dagster-gate-secrets-init
  dagster-gate-db-bootstrap
)

is_one_shot_service() {
  local candidate="$1"
  local one_shot
  for one_shot in "${ONE_SHOT_SERVICES[@]}"; do
    if [ "${candidate}" = "${one_shot}" ]; then
      return 0
    fi
  done
  return 1
}

for service in "${START_ORDER[@]}"; do
  if is_one_shot_service "${service}"; then
    compose_with_deadline "${COMPOSE_WAIT_DEADLINE}" "run ${service}" \
      up --no-build --no-deps --abort-on-container-exit \
      --exit-code-from "${service}" "${service}"
  else
    compose_with_deadline "${COMPOSE_WAIT_DEADLINE}" "start ${service}" \
      up --detach --no-build --no-deps --wait \
      --wait-timeout "${WAIT_TIMEOUT}" "${service}"
  fi
done

GRAPHQL_URL="http://127.0.0.1:${AURIS_DAGSTER_GATE_PORT}/graphql"
CALLBACK_URL="http://127.0.0.1:${AURIS_DAGSTER_GATE_CALLBACK_PORT}"

echo "Verifying real Dagster workspace, submission, terminal status and SAFE_TERMINATE..."
"${PYTHON_BIN}" "${DEADLINE_RUNNER}" \
  --timeout-seconds "${RUN_COMMAND_DEADLINE}" \
  --label "verify real Dagster initial phase" -- \
  "${PYTHON_BIN}" "${ROOT}/scripts/verify_real_dagster.py" \
  --phase initial \
  --graphql-url "${GRAPHQL_URL}" \
  --callback-url "${CALLBACK_URL}" \
  --artifact "${INITIAL_ARTIFACT}" \
  --execution-environment compose \
  --source-commit "${SOURCE_COMMIT}" \
  --run-suffix "${RUN_SUFFIX}" \
  --timeout-seconds "${RUN_TIMEOUT}" >/dev/null

echo "Restarting real Dagster code, webserver and daemon processes..."
for service in dagster-code dagster-webserver dagster-daemon; do
  compose_with_deadline "${COMPOSE_WAIT_DEADLINE}" "restart ${service}" \
    restart "${service}" >/dev/null
  compose_with_deadline "${COMPOSE_WAIT_DEADLINE}" "wait for ${service}" \
    up --detach --no-build --no-deps --wait \
    --wait-timeout "${WAIT_TIMEOUT}" "${service}" >/dev/null
done

echo "Verifying MySQL-backed terminal persistence and post-restart recovery..."
"${PYTHON_BIN}" "${DEADLINE_RUNNER}" \
  --timeout-seconds "${RUN_COMMAND_DEADLINE}" \
  --label "verify real Dagster recovery phase" -- \
  "${PYTHON_BIN}" "${ROOT}/scripts/verify_real_dagster.py" \
  --phase recovery \
  --prior-artifact "${INITIAL_ARTIFACT}" \
  --graphql-url "${GRAPHQL_URL}" \
  --callback-url "${CALLBACK_URL}" \
  --artifact "${RESULT_ARTIFACT}" \
  --execution-environment compose \
  --source-commit "${SOURCE_COMMIT}" \
  --run-suffix "${RUN_SUFFIX}" \
  --timeout-seconds "${RUN_TIMEOUT}" >/dev/null

echo "verify_real_dagster ok: ${RESULT_ARTIFACT}"
