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
DAGSTER_COMPOSE="${ROOT}/production/tests/dagster-gate.compose.yaml"
PRODUCT_COMPOSE="${ROOT}/production/tests/dagster-product-gate.compose.yaml"
RESULT_ARTIFACT="${AURIS_PRODUCT_DAGSTER_GATE_RESULT:-${ROOT}/build/release-evidence/product-dagster-gate.json}"
WAIT_TIMEOUT="${AURIS_PRODUCT_DAGSTER_GATE_WAIT_TIMEOUT:-300}"
RUN_TIMEOUT="${AURIS_PRODUCT_DAGSTER_GATE_RUN_TIMEOUT:-120}"
BUILD_TIMEOUT="${AURIS_PRODUCT_DAGSTER_GATE_BUILD_TIMEOUT:-900}"
CLEANUP_TIMEOUT="${AURIS_PRODUCT_DAGSTER_GATE_CLEANUP_TIMEOUT:-60}"
COMPOSE_DEADLINE_GRACE="${AURIS_PRODUCT_DAGSTER_GATE_DEADLINE_GRACE:-15}"
DEADLINE_RUNNER="${ROOT}/scripts/run_with_deadline.py"

if [ "${AURIS_SKIP_PRODUCT_DAGSTER_GATE:-0}" = "1" ]; then
  echo "AURIS_SKIP_PRODUCT_DAGSTER_GATE=1 is not allowed by the product Dagster gate." >&2
  exit 2
fi
if ! [[ "${WAIT_TIMEOUT}" =~ ^[1-9][0-9]*$ && \
  "${RUN_TIMEOUT}" =~ ^[1-9][0-9]*$ && \
  "${BUILD_TIMEOUT}" =~ ^[1-9][0-9]*$ && \
  "${CLEANUP_TIMEOUT}" =~ ^[1-9][0-9]*$ && \
  "${COMPOSE_DEADLINE_GRACE}" =~ ^[1-9][0-9]*$ ]]; then
  echo "Product Dagster gate timeouts must be positive integers." >&2
  exit 2
fi
COMPOSE_WAIT_DEADLINE=$((WAIT_TIMEOUT + COMPOSE_DEADLINE_GRACE))
RUN_COMMAND_DEADLINE=$((RUN_TIMEOUT + COMPOSE_DEADLINE_GRACE))
RESULT_PARENT="$(dirname -- "${RESULT_ARTIFACT}")"
RESULT_NAME="$(basename -- "${RESULT_ARTIFACT}")"
if [ "${RESULT_PARENT}" != "${ROOT}/build/release-evidence" ] || \
  ! [[ "${RESULT_NAME}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*\.json$ ]]; then
  echo "Product Dagster evidence must stay under build/release-evidence." >&2
  exit 2
fi

if ! git -C "${ROOT}" diff --quiet --; then
  echo "Product Dagster gate requires a clean worktree bound to HEAD." >&2
  exit 2
fi
if ! git -C "${ROOT}" diff --cached --quiet --; then
  echo "Product Dagster gate requires an empty Git index." >&2
  exit 2
fi
if [ -n "$(git -C "${ROOT}" ls-files --others --exclude-standard)" ]; then
  echo "Product Dagster gate refuses untracked release inputs." >&2
  exit 2
fi
SOURCE_COMMIT="$(git -C "${ROOT}" rev-parse --verify HEAD)"
if ! [[ "${SOURCE_COMMIT}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Product Dagster gate could not resolve an exact source commit." >&2
  exit 2
fi
if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  echo "Docker Engine is required for product-path real Dagster verification." >&2
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

RUN_SUFFIX="$(date +%s)-$$"
PROJECT_NAME="auris-product-dagster-gate-${RUN_SUFFIX}"
mkdir -p "${ROOT}/build/tmp"
TEMP_BASE="$(cd "${ROOT}/build/tmp" && pwd -P)"
TEMP_ROOT="$(mktemp -d "${TEMP_BASE}/auris-product-dagster-gate.XXXXXX")"
TEMP_ARTIFACT_DIR="${TEMP_ROOT}/artifacts"
TEMP_ARTIFACT="${TEMP_ARTIFACT_DIR}/product-dagster-gate.json"
COMPOSE_MODEL="${TEMP_ROOT}/compose-model.json"
mkdir -p "${TEMP_ARTIFACT_DIR}" "${TEMP_ROOT}/runtime-metrics" "${TEMP_ROOT}/secrets"
chmod 0777 "${TEMP_ARTIFACT_DIR}"

export APP_ENV="ci"
export AURIS_PRODUCT_GATE_SOURCE_COMMIT="${SOURCE_COMMIT}"
export AURIS_PRODUCT_DAGSTER_GATE_RUN_SUFFIX="${RUN_SUFFIX}"
export AURIS_PRODUCT_DAGSTER_GATE_RUN_TIMEOUT="${RUN_TIMEOUT}"
export AURIS_PRODUCT_DAGSTER_GATE_ARTIFACT_DIR="${TEMP_ARTIFACT_DIR}"
export AURIS_PRODUCT_DAGSTER_GATE_BFF_PORT="${AURIS_PRODUCT_DAGSTER_GATE_BFF_PORT:-$(free_port)}"
export AURIS_DAGSTER_GATE_PORT="${AURIS_DAGSTER_GATE_PORT:-$(free_port)}"
export AURIS_DAGSTER_GATE_CALLBACK_PORT="${AURIS_DAGSTER_GATE_CALLBACK_PORT:-$(free_port)}"
export AURIS_SECRETS_DIR="${TEMP_ROOT}/secrets"
export AURIS_RUNTIME_METRICS_DIR="${TEMP_ROOT}/runtime-metrics"
export AURIS_PUBLIC_HOST="auris-product-dagster-gate.invalid"
export AURIS_EXTERNAL_CALLBACK_URL="https://callback.invalid/callbacks/auris-flow"
export AURIS_EXTERNAL_CALLBACK_HOST="callback.invalid"
export AURIS_EMBEDDING_ENDPOINT="https://embedding.invalid/v1/embeddings"
export AURIS_EMBEDDING_MODEL="product-dagster-gate-unused"
export AURIS_EMBEDDING_DIMENSION="8"
export AURIS_AUDIO_INFERENCE_PROVIDER="audio_intelligence_default"
export AURIS_AUDIO_INFERENCE_ALLOWED_MODELS="audio-v2.3.1"
export AURIS_AUDIO_INFERENCE_ENDPOINT="https://audio-inference.invalid/v1/audio-intelligence"
export AURIS_BFF_IMAGE="auris-flow-product-dagster-gate-bff:${RUN_SUFFIX}"
export AURIS_DAGSTER_IMAGE="auris-flow-product-dagster-gate-engine:${RUN_SUFFIX}"

COMPOSE=(
  docker compose
  --parallel=1
  --project-name "${PROJECT_NAME}"
  --project-directory "${ROOT}/production"
  --file "${BASE_COMPOSE}"
  --file "${DAGSTER_COMPOSE}"
  --file "${PRODUCT_COMPOSE}"
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
  local status="$?" cleanup_failed=0
  trap - EXIT INT TERM
  if [ "${status}" -ne 0 ]; then
    compose_with_deadline "${CLEANUP_TIMEOUT}" "collect product Dagster logs" \
      logs --tail 120 \
      bff worker dagster-code dagster-webserver dagster-daemon >&2 || true
  fi
  if [[ "${PROJECT_NAME}" =~ ^auris-product-dagster-gate-[0-9]+-[0-9]+$ ]]; then
    if ! compose_with_deadline "${CLEANUP_TIMEOUT}" "clean product Dagster project" \
      down --volumes --remove-orphans --timeout 20 >/dev/null 2>&1; then
      echo "Could not clean the isolated product Dagster gate project." >&2
      cleanup_failed=1
    fi
  else
    echo "Refusing to clean an unexpected Compose project." >&2
    cleanup_failed=1
  fi
  if [[ "${TEMP_ROOT}" == "${TEMP_BASE}"/auris-product-dagster-gate.* ]] && [ -d "${TEMP_ROOT}" ]; then
    if ! rm -rf -- "${TEMP_ROOT}"; then
      echo "Could not remove the isolated product Dagster gate directory." >&2
      cleanup_failed=1
    fi
  else
    echo "Refusing to remove an unexpected product gate directory." >&2
    cleanup_failed=1
  fi
  if [ "${status}" -eq 0 ] && [ "${cleanup_failed}" -ne 0 ]; then
    status=1
  fi
  exit "${status}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

compose_with_deadline "${CLEANUP_TIMEOUT}" "validate product Dagster Compose" config --quiet
compose_with_deadline "${CLEANUP_TIMEOUT}" "render product Dagster Compose" config --format json >"${COMPOSE_MODEL}"
"${PYTHON_BIN}" "${DEADLINE_RUNNER}" \
  --timeout-seconds "${CLEANUP_TIMEOUT}" \
  --label "validate product Dagster host network" -- \
  "${PYTHON_BIN}" "${ROOT}/scripts/verify_dagster_gate_network.py" \
  --compose-model "${COMPOSE_MODEL}" \
  --project-name "${PROJECT_NAME}" \
  --webserver-port "${AURIS_DAGSTER_GATE_PORT}" \
  --callback-port "${AURIS_DAGSTER_GATE_CALLBACK_PORT}" >/dev/null
mkdir -p "$(dirname "${RESULT_ARTIFACT}")"
rm -f -- "${RESULT_ARTIFACT}"

echo "Building isolated BFF and real Dagster images for the product gate..."
compose_with_deadline "${BUILD_TIMEOUT}" "build product Dagster gate images" \
  build bff dagster-code

START_ORDER=(
  dagster-gate-secrets-init
  mysql
  redis
  dagster-gate-db-bootstrap
  dagster-product-gate-db-bootstrap
  migrate
  dagster-product-gate-seed
  dagster-code
  dagster-webserver
  dagster-daemon
  bff
  worker
)
ONE_SHOT_SERVICES=(
  dagster-gate-secrets-init
  dagster-gate-db-bootstrap
  dagster-product-gate-db-bootstrap
  migrate
  dagster-product-gate-seed
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

echo "Exercising BFF submit/query/sync/cancel through the real Dagster deployment..."
compose_with_deadline "${RUN_COMMAND_DEADLINE}" "run product Dagster verifier" \
  run --rm --no-deps dagster-product-gate-verifier >/dev/null
if [ ! -f "${TEMP_ARTIFACT}" ]; then
  echo "Product Dagster verifier did not produce evidence." >&2
  exit 1
fi

"${PYTHON_BIN}" - "${TEMP_ARTIFACT}" "${SOURCE_COMMIT}" <<'PY'
import json
import sys
from pathlib import Path

artifact = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if (
    artifact.get("schema_version") != "auris.product-dagster-gate.v1"
    or artifact.get("status") != "ok"
    or artifact.get("source_commit") != sys.argv[2]
    or artifact.get("execution_environment") != "compose"
    or artifact.get("adapter_mode") != "real"
):
    raise SystemExit("Product Dagster evidence envelope is invalid.")
PY
install -m 0644 "${TEMP_ARTIFACT}" "${RESULT_ARTIFACT}"

echo "verify_product_dagster_path ok: ${RESULT_ARTIFACT}"
