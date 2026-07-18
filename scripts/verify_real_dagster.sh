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
RESULT_ARTIFACT="${AURIS_REAL_DAGSTER_RESULT:-${ROOT}/build/release-evidence/real-dagster-gate.json}"
WAIT_TIMEOUT="${AURIS_REAL_DAGSTER_WAIT_TIMEOUT:-240}"
RUN_TIMEOUT="${AURIS_REAL_DAGSTER_RUN_TIMEOUT:-90}"
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

cleanup() {
  local status="$?"
  trap - EXIT INT TERM
  if [ "${status}" -ne 0 ]; then
    "${COMPOSE[@]}" logs --tail 120 \
      dagster-gate-callback dagster-code dagster-webserver dagster-daemon >&2 || true
  fi
  if [[ "${PROJECT_NAME}" =~ ^auris-dagster-gate-[0-9]+-[0-9]+$ ]]; then
    if ! "${COMPOSE[@]}" down --volumes --remove-orphans --timeout 20 >/dev/null 2>&1; then
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
if ! [[ "${WAIT_TIMEOUT}" =~ ^[1-9][0-9]*$ && "${RUN_TIMEOUT}" =~ ^[1-9][0-9]*$ ]]; then
  echo "Real Dagster timeout values must be positive integers." >&2
  exit 2
fi

AURIS_SECRETS_DIR="${AURIS_SECRETS_DIR}" \
  AURIS_RUNTIME_METRICS_DIR="${AURIS_RUNTIME_METRICS_DIR}" \
  bash "${ROOT}/production/scripts/init-secrets.sh" >/dev/null

"${COMPOSE[@]}" config --quiet
mkdir -p "$(dirname "${RESULT_ARTIFACT}")"
rm -f "${RESULT_ARTIFACT}"

echo "Starting isolated production Dagster services (${PROJECT_NAME})..."
"${COMPOSE[@]}" build dagster-code
"${COMPOSE[@]}" build dagster-gate-callback
START_ORDER=(
  dagster-gate-secrets-init
  mysql
  dagster-gate-callback
  dagster-gate-db-bootstrap
  dagster-code
  dagster-webserver
  dagster-daemon
)
for service in "${START_ORDER[@]}"; do
  "${COMPOSE[@]}" up --detach --no-build --wait \
    --wait-timeout "${WAIT_TIMEOUT}" "${service}"
done

GRAPHQL_URL="http://127.0.0.1:${AURIS_DAGSTER_GATE_PORT}/graphql"
CALLBACK_URL="http://127.0.0.1:${AURIS_DAGSTER_GATE_CALLBACK_PORT}"

echo "Verifying real Dagster workspace, submission, terminal status and SAFE_TERMINATE..."
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
  "${COMPOSE[@]}" restart "${service}" >/dev/null
  "${COMPOSE[@]}" up --detach --no-build --wait \
    --wait-timeout "${WAIT_TIMEOUT}" "${service}" >/dev/null
done

echo "Verifying MySQL-backed terminal persistence and post-restart recovery..."
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
