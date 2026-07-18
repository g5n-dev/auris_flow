#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

if [ -n "${PYTHON:-}" ]; then
  PYTHON_BIN="${PYTHON}"
elif [ -x "${ROOT}/backend/.venv/bin/python" ]; then
  PYTHON_BIN="${ROOT}/backend/.venv/bin/python"
else
  PYTHON_BIN="$(command -v python3)"
fi

if [ ! -x "${PYTHON_BIN}" ]; then
  echo "Python not found. Create backend/.venv or set PYTHON=/absolute/path/to/python." >&2
  exit 2
fi

if [ ! -d "${ROOT}/prototype/auris-flow-ui/node_modules" ]; then
  echo "Frontend dependencies missing. Run: npm ci --prefix prototype/auris-flow-ui" >&2
  exit 2
fi

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

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/auris-ui-audit.XXXXXX")"
BFF_PORT="${AURIS_AUDIT_BFF_PORT:-$(free_port)}"
UI_PORT="${AURIS_AUDIT_UI_PORT:-$(free_port)}"
DB_URL="sqlite:///${TMP_DIR}/auris_audit.sqlite"
BFF_URL="http://127.0.0.1:${BFF_PORT}"
UI_URL="http://127.0.0.1:${UI_PORT}/"
BFF_PID=""
UI_PID=""

cleanup() {
  local status=$?
  if [ -n "${UI_PID}" ] && kill -0 "${UI_PID}" 2>/dev/null; then
    kill "${UI_PID}" 2>/dev/null || true
    wait "${UI_PID}" 2>/dev/null || true
  fi
  if [ -n "${BFF_PID}" ] && kill -0 "${BFF_PID}" 2>/dev/null; then
    kill "${BFF_PID}" 2>/dev/null || true
    wait "${BFF_PID}" 2>/dev/null || true
  fi
  rm -rf "${TMP_DIR}"
  exit "${status}"
}
trap cleanup EXIT INT TERM

(
  cd backend
  DATABASE_URL="${DB_URL}" "${PYTHON_BIN}" -m alembic upgrade head >/dev/null
  DATABASE_URL="${DB_URL}" "${PYTHON_BIN}" -m app.seed local_demo >/dev/null
)

(
  cd backend
  env \
    APP_ENV=local \
    ALLOW_DEV_AUTH=true \
    "DATABASE_URL=${DB_URL}" \
    "COMPLETION_RECEIPT_SECRET=${COMPLETION_RECEIPT_SECRET:-auris-e2e-completion-secret-32chars-minimum}" \
    "AUDIO_PLAYBACK_GRANT_SECRET=${AUDIO_PLAYBACK_GRANT_SECRET:-auris-e2e-playback-grant-secret-32chars-minimum}" \
    "${PYTHON_BIN}" -m uvicorn app.main:app \
      --host 127.0.0.1 --port "${BFF_PORT}" --no-access-log >"${TMP_DIR}/bff.log" 2>&1
) &
BFF_PID=$!
wait_for_url "${BFF_URL}/healthz" "BFF" "${TMP_DIR}/bff.log"

(
  cd prototype/auris-flow-ui
  VITE_API_PROXY_TARGET="${BFF_URL}" npm exec vite -- \
    --host 127.0.0.1 --port "${UI_PORT}" --strictPort >"${TMP_DIR}/vite.log" 2>&1
) &
UI_PID=$!
wait_for_url "${UI_URL}" "UI" "${TMP_DIR}/vite.log"
wait_for_url "${UI_URL%/}/healthz" "UI proxy" "${TMP_DIR}/vite.log"

echo "Running UI audits against ${UI_URL}"
AURIS_AUDIT_URL="${UI_URL}" npm --prefix prototype/auris-flow-ui run audit:tabs
AURIS_AUDIT_URL="${UI_URL}" npm --prefix prototype/auris-flow-ui run audit:capture
echo "audit_ui ok: ${UI_URL}"
