#!/usr/bin/env bash
set -euo pipefail

# Supplemental real `dagster dev` process proof only. This script deliberately
# cannot write the Compose release artifact used by scripts/verify_release.sh.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_PYTHON="${ROOT}/backend/.venv/bin/python"
DAGSTER_BIN="${ROOT}/production/dagster/.venv/bin/dagster"
WORKSPACE_CONFIG="${ROOT}/production/tests/dagster-local-process.workspace.yaml"
DAGSTER_CONFIG="${ROOT}/production/tests/dagster-local-process.dagster.yaml"
RESULT_ARTIFACT="${ROOT}/build/diagnostics/real-dagster-local-process.json"
RUN_SUFFIX="$(date +%s)-$$"
SOURCE_COMMIT="$(git -C "${ROOT}" rev-parse --verify HEAD^{commit})"

mkdir -p "${ROOT}/build/tmp"
TEMP_BASE="$(cd "${ROOT}/build/tmp" && pwd -P)"
TEMP_ROOT="$(mktemp -d "${TEMP_BASE}/auris-dagster-local-process.XXXXXX")"
INITIAL_ARTIFACT="${TEMP_ROOT}/initial.json"
DAGSTER_LOG="${TEMP_ROOT}/dagster.log"
CALLBACK_LOG="${TEMP_ROOT}/callback.log"
DAGSTER_PID=""
CALLBACK_PID=""

free_port() {
  "${BACKEND_PYTHON}" - <<'PY'
import socket

with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
}

stop_process() {
  local pid="$1"
  if [ -z "${pid}" ] || ! kill -0 "${pid}" 2>/dev/null; then
    return 0
  fi
  kill -TERM "${pid}" 2>/dev/null || true
  for _ in $(seq 1 100); do
    if ! kill -0 "${pid}" 2>/dev/null; then
      wait "${pid}" 2>/dev/null || true
      return 0
    fi
    sleep 0.1
  done
  kill -KILL "${pid}" 2>/dev/null || true
  wait "${pid}" 2>/dev/null || true
}

cleanup() {
  local status="$?"
  trap - EXIT INT TERM
  if [ "${status}" -ne 0 ]; then
    tail -n 120 "${DAGSTER_LOG}" >&2 2>/dev/null || true
    tail -n 80 "${CALLBACK_LOG}" >&2 2>/dev/null || true
  fi
  stop_process "${DAGSTER_PID}"
  stop_process "${CALLBACK_PID}"
  if [[ "${TEMP_ROOT}" == "${TEMP_BASE}"/auris-dagster-local-process.* ]] && [ -d "${TEMP_ROOT}" ]; then
    rm -rf -- "${TEMP_ROOT}"
  else
    echo "Refusing to remove unexpected local-process directory: ${TEMP_ROOT}" >&2
    status=1
  fi
  exit "${status}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [ ! -x "${BACKEND_PYTHON}" ] || [ ! -x "${DAGSTER_BIN}" ]; then
  echo "Locked backend and production Dagster virtual environments are required." >&2
  exit 2
fi

GRAPHQL_PORT="$(free_port)"
CALLBACK_PORT="$(free_port)"
GRAPHQL_URL="http://127.0.0.1:${GRAPHQL_PORT}/graphql"
CALLBACK_URL="http://127.0.0.1:${CALLBACK_PORT}"
export APP_ENV="ci"
export PYTHONPATH="${ROOT}/production/dagster/src${PYTHONPATH:+:${PYTHONPATH}}"
export DAGSTER_HOME="${TEMP_ROOT}/dagster-home"
export AURIS_SECRETS_DIR="${TEMP_ROOT}/secrets"
export AURIS_RUNTIME_METRICS_DIR="${TEMP_ROOT}/runtime-metrics"
export AURIS_BFF_INTERNAL_URL="${CALLBACK_URL}"
export AURIS_COMPLETION_RECEIPT_KEYRING_FILE="${AURIS_SECRETS_DIR}/completion_receipt_key_bindings"
export AURIS_COMPLETION_RECEIPT_ACTIVE_KEY_ID="dagster-v1"
export OTEL_ENABLED="false"
export PYTHONUNBUFFERED="1"

mkdir -p "${DAGSTER_HOME}" "$(dirname "${RESULT_ARTIFACT}")"
cp "${DAGSTER_CONFIG}" "${DAGSTER_HOME}/dagster.yaml"
bash "${ROOT}/production/scripts/init-secrets.sh" >/dev/null
rm -f "${RESULT_ARTIFACT}"

"${BACKEND_PYTHON}" "${ROOT}/scripts/verify_real_dagster_callback_server.py" \
  --host 127.0.0.1 \
  --port "${CALLBACK_PORT}" \
  --keyring-file "${AURIS_COMPLETION_RECEIPT_KEYRING_FILE}" \
  >"${CALLBACK_LOG}" 2>&1 &
CALLBACK_PID="$!"

start_dagster() {
  echo "Starting supplemental real dagster dev process..."
  "${DAGSTER_BIN}" dev \
    --workspace "${WORKSPACE_CONFIG}" \
    --host 127.0.0.1 \
    --port "${GRAPHQL_PORT}" \
    --log-level info \
    --code-server-log-level info \
    --verbose \
    >"${DAGSTER_LOG}" 2>&1 &
  DAGSTER_PID="$!"
}

start_dagster
"${BACKEND_PYTHON}" "${ROOT}/scripts/verify_real_dagster.py" \
  --phase initial \
  --graphql-url "${GRAPHQL_URL}" \
  --callback-url "${CALLBACK_URL}" \
  --artifact "${INITIAL_ARTIFACT}" \
  --execution-environment local-process \
  --source-commit "${SOURCE_COMMIT}" \
  --run-suffix "${RUN_SUFFIX}" \
  --timeout-seconds 120 >/dev/null

echo "Restarting supplemental real Dagster processes with the same local storage..."
stop_process "${DAGSTER_PID}"
DAGSTER_PID=""
start_dagster
"${BACKEND_PYTHON}" "${ROOT}/scripts/verify_real_dagster.py" \
  --phase recovery \
  --prior-artifact "${INITIAL_ARTIFACT}" \
  --graphql-url "${GRAPHQL_URL}" \
  --callback-url "${CALLBACK_URL}" \
  --artifact "${RESULT_ARTIFACT}" \
  --execution-environment local-process \
  --source-commit "${SOURCE_COMMIT}" \
  --run-suffix "${RUN_SUFFIX}" \
  --timeout-seconds 120 >/dev/null

echo "Supplemental local-process evidence only (not Compose release proof): ${RESULT_ARTIFACT}"
