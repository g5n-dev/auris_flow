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

if [ "${AURIS_SKIP_PRODUCT_DAGSTER_GATE:-0}" = "1" ]; then
  echo "AURIS_SKIP_PRODUCT_DAGSTER_GATE=1 is not allowed by the product Dagster gate." >&2
  exit 2
fi
if ! [[ "${WAIT_TIMEOUT}" =~ ^[1-9][0-9]*$ && "${RUN_TIMEOUT}" =~ ^[1-9][0-9]*$ ]]; then
  echo "Product Dagster gate timeouts must be positive integers." >&2
  exit 2
fi
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

cleanup() {
  local status="$?"
  trap - EXIT INT TERM
  if [ "${status}" -ne 0 ]; then
    "${COMPOSE[@]}" logs --tail 120 \
      bff worker dagster-code dagster-webserver dagster-daemon >&2 || true
  fi
  if [[ "${PROJECT_NAME}" =~ ^auris-product-dagster-gate-[0-9]+-[0-9]+$ ]]; then
    if ! "${COMPOSE[@]}" down --volumes --remove-orphans --timeout 20 >/dev/null 2>&1; then
      echo "Could not clean the isolated product Dagster gate project." >&2
      status=1
    fi
  else
    echo "Refusing to clean an unexpected Compose project." >&2
    status=1
  fi
  if [[ "${TEMP_ROOT}" == "${TEMP_BASE}"/auris-product-dagster-gate.* ]] && [ -d "${TEMP_ROOT}" ]; then
    rm -rf -- "${TEMP_ROOT}"
  else
    echo "Refusing to remove an unexpected product gate directory." >&2
    status=1
  fi
  exit "${status}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

"${COMPOSE[@]}" config --quiet
mkdir -p "$(dirname "${RESULT_ARTIFACT}")"
rm -f -- "${RESULT_ARTIFACT}"

echo "Building isolated BFF and real Dagster images for the product gate..."
"${COMPOSE[@]}" build bff dagster-code

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
for service in "${START_ORDER[@]}"; do
  "${COMPOSE[@]}" up --detach --no-build --wait \
    --wait-timeout "${WAIT_TIMEOUT}" "${service}"
done

echo "Exercising BFF submit/query/sync/cancel through the real Dagster deployment..."
"${COMPOSE[@]}" run --rm --no-deps dagster-product-gate-verifier >/dev/null
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
