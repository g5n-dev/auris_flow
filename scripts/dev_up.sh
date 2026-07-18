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

if ! "${PYTHON_BIN}" - <<'PY'
import importlib.util
import sys

missing = [
    module
    for module in ("fastapi", "sqlalchemy", "alembic", "uvicorn")
    if importlib.util.find_spec(module) is None
]
if missing:
    print("Missing backend dependencies: " + ", ".join(missing), file=sys.stderr)
    sys.exit(1)
PY
then
  echo 'Install backend dependencies with: python -m pip install -e "backend[dev]"' >&2
  exit 2
fi

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  docker compose -f docker/local/docker-compose.yml up -d mysql redis minio qdrant
else
  echo "Docker is not available; continuing without starting MySQL/Redis/MinIO/Qdrant." >&2
fi

(
  cd backend
  "${PYTHON_BIN}" -m alembic upgrade head
  "${PYTHON_BIN}" -m app.seed local_demo
)

RUNTIME_DIR="$(mktemp -d "${TMPDIR:-/tmp}/auris-flow-dev.XXXXXX")"
WORKER_HEALTH_PATH="${RUNTIME_DIR}/outbox-worker-health.json"
pids=()
process_names=()
cleanup() {
  local status=$?
  trap - EXIT
  for pid in "${pids[@]}"; do
    if kill -0 "${pid}" 2>/dev/null; then
      kill "${pid}" 2>/dev/null || true
    fi
  done
  for pid in "${pids[@]}"; do
    wait "${pid}" 2>/dev/null || true
  done
  rm -rf "${RUNTIME_DIR}"
  return "${status}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

wait_for_worker() {
  local pid="$1"
  for _ in $(seq 1 100); do
    if ! kill -0 "${pid}" 2>/dev/null; then
      echo "Outbox worker exited before becoming healthy." >&2
      return 1
    fi
    if [ -f "${WORKER_HEALTH_PATH}" ] && "${PYTHON_BIN}" - "${WORKER_HEALTH_PATH}" <<'PY'
import json
import sys
from pathlib import Path

try:
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except (OSError, ValueError):
    raise SystemExit(1)
raise SystemExit(
    0
    if payload.get("status") == "running" and payload.get("healthy") is True
    else 1
)
PY
    then
      return 0
    fi
    sleep 0.1
  done
  echo "Outbox worker did not publish a healthy state: ${WORKER_HEALTH_PATH}" >&2
  return 1
}

supervise() {
  local index pid name status
  while true; do
    for ((index = 0; index < ${#pids[@]}; index += 1)); do
      pid="${pids[index]}"
      name="${process_names[index]}"
      if ! kill -0 "${pid}" 2>/dev/null; then
        status=0
        wait "${pid}" || status=$?
        if [ "${status}" -eq 0 ]; then
          status=1
        fi
        echo "${name} exited unexpectedly (status ${status}); stopping the dev stack." >&2
        return "${status}"
      fi
    done
    sleep 1
  done
}

(
  cd backend
  exec env APP_ENV=local ALLOW_DEV_AUTH=true "${PYTHON_BIN}" -m uvicorn app.main:app \
    --host 127.0.0.1 --port "${AURIS_BFF_PORT:-8000}" --reload --no-access-log
) &
pids+=("$!")
process_names+=("BFF")

(
  cd backend
  exec env APP_ENV=local ALLOW_DEV_AUTH=true \
    AURIS_OUTBOX_HEALTH_PATH="${WORKER_HEALTH_PATH}" \
    "${PYTHON_BIN}" -m app.workers.outbox_worker
) &
WORKER_PID="$!"
pids+=("${WORKER_PID}")
process_names+=("Outbox worker")
wait_for_worker "${WORKER_PID}"

(
  cd prototype/auris-flow-ui
  # Canonical package entrypoint: npm --prefix prototype/auris-flow-ui run dev
  exec ./node_modules/.bin/vite --host 127.0.0.1 --port 5173 --strictPort
) &
pids+=("$!")
process_names+=("Vite")

cat <<EOF
Auris Flow dev stack is starting.

BFF:      http://127.0.0.1:${AURIS_BFF_PORT:-8000}
Health:   http://127.0.0.1:${AURIS_BFF_PORT:-8000}/readyz
Frontend: http://127.0.0.1:5173
Worker:   ${WORKER_HEALTH_PATH}

Press Ctrl-C to stop BFF, worker, and Vite.
EOF

supervise
