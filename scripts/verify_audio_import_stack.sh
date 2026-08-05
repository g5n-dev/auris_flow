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
AUDIO_IMPORT_COMPOSE="${ROOT}/production/tests/audio-import-gate.compose.yaml"
RESULT_ARTIFACT="${AURIS_AUDIO_IMPORT_GATE_RESULT:-${ROOT}/build/release-evidence/audio-import-real-stack-gate.json}"
BROWSER_RESULT_ARTIFACT="${AURIS_AUDIO_IMPORT_BROWSER_RESULT:-${ROOT}/build/release-evidence/audio-import-browser-e2e.json}"
WAIT_TIMEOUT="${AURIS_AUDIO_IMPORT_GATE_WAIT_TIMEOUT:-300}"
RUN_TIMEOUT="${AURIS_AUDIO_IMPORT_GATE_RUN_TIMEOUT:-180}"
BROWSER_TIMEOUT="${AURIS_AUDIO_IMPORT_GATE_BROWSER_TIMEOUT:-360}"
BUILD_TIMEOUT="${AURIS_AUDIO_IMPORT_GATE_BUILD_TIMEOUT:-900}"
CLEANUP_TIMEOUT="${AURIS_AUDIO_IMPORT_GATE_CLEANUP_TIMEOUT:-60}"
DEADLINE_GRACE="${AURIS_AUDIO_IMPORT_GATE_DEADLINE_GRACE:-15}"
DEADLINE_RUNNER="${ROOT}/scripts/run_with_deadline.py"

if [ "${AURIS_SKIP_AUDIO_IMPORT_REAL_STACK_GATE:-0}" = "1" ]; then
  echo "AURIS_SKIP_AUDIO_IMPORT_REAL_STACK_GATE=1 is not allowed by this gate." >&2
  exit 2
fi
if ! [[ "${WAIT_TIMEOUT}" =~ ^[1-9][0-9]*$ && \
  "${RUN_TIMEOUT}" =~ ^[1-9][0-9]*$ && \
  "${BROWSER_TIMEOUT}" =~ ^[1-9][0-9]*$ && \
  "${BUILD_TIMEOUT}" =~ ^[1-9][0-9]*$ && \
  "${CLEANUP_TIMEOUT}" =~ ^[1-9][0-9]*$ && \
  "${DEADLINE_GRACE}" =~ ^[1-9][0-9]*$ ]]; then
  echo "Audio import real-stack gate timeouts must be positive integers." >&2
  exit 2
fi
COMPOSE_WAIT_DEADLINE=$((WAIT_TIMEOUT + DEADLINE_GRACE))
RUN_COMMAND_DEADLINE=$((RUN_TIMEOUT + DEADLINE_GRACE))
BROWSER_COMMAND_DEADLINE=$((BROWSER_TIMEOUT + DEADLINE_GRACE))

RESULT_PARENT="$(dirname -- "${RESULT_ARTIFACT}")"
RESULT_NAME="$(basename -- "${RESULT_ARTIFACT}")"
if [ "${RESULT_PARENT}" != "${ROOT}/build/release-evidence" ] || \
  ! [[ "${RESULT_NAME}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*\.json$ ]]; then
  echo "Audio import evidence must stay under build/release-evidence." >&2
  exit 2
fi
BROWSER_RESULT_PARENT="$(dirname -- "${BROWSER_RESULT_ARTIFACT}")"
BROWSER_RESULT_NAME="$(basename -- "${BROWSER_RESULT_ARTIFACT}")"
if [ "${BROWSER_RESULT_PARENT}" != "${ROOT}/build/release-evidence" ] || \
  ! [[ "${BROWSER_RESULT_NAME}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*\.json$ ]]; then
  echo "Audio import browser evidence must stay under build/release-evidence." >&2
  exit 2
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "Docker Engine is required for the audio import real-stack gate." >&2
  exit 2
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker Engine is unavailable for the audio import real-stack gate." >&2
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

SOURCE_COMMIT="$(git -C "${ROOT}" rev-parse --verify HEAD)"
if ! [[ "${SOURCE_COMMIT}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Audio import gate could not resolve an exact source commit." >&2
  exit 2
fi
SOURCE_TREE_DIRTY=false
if ! git -C "${ROOT}" diff --quiet -- || \
  ! git -C "${ROOT}" diff --cached --quiet -- || \
  [ -n "$(git -C "${ROOT}" ls-files --others --exclude-standard)" ]; then
  SOURCE_TREE_DIRTY=true
fi

RUN_SUFFIX="$(date +%s)-$$"
PROJECT_NAME="auris-audio-import-gate-${RUN_SUFFIX}"
mkdir -p "${ROOT}/build/tmp"
TEMP_BASE="$(cd "${ROOT}/build/tmp" && pwd -P)"
TEMP_ROOT="$(mktemp -d "${TEMP_BASE}/auris-audio-import-gate.XXXXXX")"
TEMP_ARTIFACT_DIR="${TEMP_ROOT}/artifacts"
TEMP_ARTIFACT="${TEMP_ARTIFACT_DIR}/audio-import-real-stack-gate.json"
TEMP_BROWSER_ARTIFACT="${TEMP_ARTIFACT_DIR}/audio-import-browser-e2e.json"
COMPOSE_MODEL="${TEMP_ROOT}/compose-model.json"
mkdir -p \
  "${TEMP_ARTIFACT_DIR}" \
  "${TEMP_ROOT}/runtime-metrics" \
  "${TEMP_ROOT}/secrets"
chmod 0777 "${TEMP_ARTIFACT_DIR}"

export APP_ENV="ci"
export AURIS_AUDIO_IMPORT_GATE_SOURCE_COMMIT="${SOURCE_COMMIT}"
export AURIS_AUDIO_IMPORT_GATE_SOURCE_TREE_DIRTY="${SOURCE_TREE_DIRTY}"
export AURIS_AUDIO_IMPORT_GATE_RUN_SUFFIX="${RUN_SUFFIX}"
export AURIS_AUDIO_IMPORT_GATE_RUN_TIMEOUT="${RUN_TIMEOUT}"
export AURIS_AUDIO_IMPORT_GATE_ARTIFACT_DIR="${TEMP_ARTIFACT_DIR}"
export AURIS_AUDIO_IMPORT_GATE_BFF_PORT="${AURIS_AUDIO_IMPORT_GATE_BFF_PORT:-$(free_port)}"
export AURIS_PRODUCT_GATE_SOURCE_COMMIT="${SOURCE_COMMIT}"
export AURIS_PRODUCT_DAGSTER_GATE_RUN_SUFFIX="${RUN_SUFFIX}"
export AURIS_PRODUCT_DAGSTER_GATE_RUN_TIMEOUT="${RUN_TIMEOUT}"
export AURIS_PRODUCT_DAGSTER_GATE_ARTIFACT_DIR="${TEMP_ARTIFACT_DIR}"
export AURIS_PRODUCT_DAGSTER_GATE_BFF_PORT="${AURIS_AUDIO_IMPORT_GATE_BFF_PORT}"
export AURIS_DAGSTER_GATE_PORT="${AURIS_DAGSTER_GATE_PORT:-$(free_port)}"
export AURIS_DAGSTER_GATE_CALLBACK_PORT="${AURIS_DAGSTER_GATE_CALLBACK_PORT:-$(free_port)}"
export AURIS_SECRETS_DIR="${TEMP_ROOT}/secrets"
export AURIS_RUNTIME_METRICS_DIR="${TEMP_ROOT}/runtime-metrics"
export AURIS_PUBLIC_HOST="auris-audio-import-gate.invalid"
export AURIS_EXTERNAL_CALLBACK_URL="https://callback.invalid/callbacks/auris-flow"
export AURIS_EXTERNAL_CALLBACK_HOST="callback.invalid"
export AURIS_EMBEDDING_ENDPOINT="https://embedding.invalid/v1/embeddings"
export AURIS_EMBEDDING_MODEL="audio-import-gate-unused"
export AURIS_EMBEDDING_DIMENSION="8"
export AURIS_AUDIO_INFERENCE_PROVIDER="audio_intelligence_default"
export AURIS_AUDIO_INFERENCE_ALLOWED_MODELS="audio-v2.3.1"
export AURIS_AUDIO_INFERENCE_ENDPOINT="https://audio-inference.invalid/v1/audio-intelligence"
export AURIS_BFF_IMAGE="auris-flow-audio-import-gate-bff:${RUN_SUFFIX}"
export AURIS_DAGSTER_IMAGE="auris-flow-audio-import-gate-engine:${RUN_SUFFIX}"

COMPOSE=(
  docker compose
  --parallel=1
  --project-name "${PROJECT_NAME}"
  --project-directory "${ROOT}/production"
  --file "${BASE_COMPOSE}"
  --file "${DAGSTER_COMPOSE}"
  --file "${PRODUCT_COMPOSE}"
  --file "${AUDIO_IMPORT_COMPOSE}"
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
    compose_with_deadline "${CLEANUP_TIMEOUT}" "collect audio import gate logs" \
      logs --tail 160 \
      audio-import-platform audio-import-inference minio minio-bootstrap \
      bff worker dagster-code dagster-webserver dagster-daemon >&2 || true
  fi
  if [[ "${PROJECT_NAME}" =~ ^auris-audio-import-gate-[0-9]+-[0-9]+$ ]]; then
    if ! compose_with_deadline "${CLEANUP_TIMEOUT}" "clean audio import gate project" \
      down --volumes --remove-orphans --timeout 20 >/dev/null 2>&1; then
      echo "Could not clean the isolated audio import gate project." >&2
      cleanup_failed=1
    fi
  else
    echo "Refusing to clean an unexpected Compose project." >&2
    cleanup_failed=1
  fi
  if [[ "${TEMP_ROOT}" == "${TEMP_BASE}"/auris-audio-import-gate.* ]] && \
    [ -d "${TEMP_ROOT}" ]; then
    if ! rm -rf -- "${TEMP_ROOT}"; then
      echo "Could not remove the isolated audio import gate directory." >&2
      cleanup_failed=1
    fi
  else
    echo "Refusing to remove an unexpected audio import gate directory." >&2
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

compose_with_deadline "${CLEANUP_TIMEOUT}" \
  "validate audio import gate Compose" config --quiet
compose_with_deadline "${CLEANUP_TIMEOUT}" \
  "render audio import gate Compose" config --format json >"${COMPOSE_MODEL}"
"${PYTHON_BIN}" "${DEADLINE_RUNNER}" \
  --timeout-seconds "${CLEANUP_TIMEOUT}" \
  --label "validate audio import gate topology" -- \
  "${PYTHON_BIN}" "${ROOT}/scripts/verify_audio_import_gate_compose.py" \
  --compose-model "${COMPOSE_MODEL}" >/dev/null

mkdir -p "$(dirname "${RESULT_ARTIFACT}")"
rm -f -- "${RESULT_ARTIFACT}" "${BROWSER_RESULT_ARTIFACT}"

echo "Building isolated BFF and Dagster images for the real audio import gate..."
compose_with_deadline "${BUILD_TIMEOUT}" \
  "build audio import gate images" build bff dagster-code

START_ORDER=(
  dagster-gate-secrets-init
  audio-import-gate-secrets-augment
  audio-import-gate-pki-init
  minio-volume-init
  mysql
  redis
  minio
  minio-bootstrap
  dagster-gate-db-bootstrap
  dagster-storage-bootstrap
  dagster-product-gate-db-bootstrap
  migrate
  dagster-product-gate-seed
  audio-import-gate-platform-connection-seed
  audio-import-platform
  audio-import-inference
  dagster-code
  dagster-webserver
  dagster-daemon
  bff
  worker
)
ONE_SHOT_SERVICES=(
  dagster-gate-secrets-init
  audio-import-gate-secrets-augment
  audio-import-gate-pki-init
  minio-volume-init
  minio-bootstrap
  dagster-gate-db-bootstrap
  dagster-storage-bootstrap
  dagster-product-gate-db-bootstrap
  migrate
  dagster-product-gate-seed
  audio-import-gate-platform-connection-seed
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

echo "Exercising browser UI -> BFF -> HTTPS platform -> Dagster -> MinIO -> playback -> intelligence -> evidence -> review -> trace..."
BFF_HOST_BINDING="$(
  compose_with_deadline "${CLEANUP_TIMEOUT}" \
    "resolve audio import gate BFF port" port bff 8000
)"
BROWSER_BFF_PORT="$(
  "${PYTHON_BIN}" - "${BFF_HOST_BINDING}" <<'PY'
import re
import sys

binding = sys.argv[1].strip()
match = re.fullmatch(r"127\.0\.0\.1:([1-9][0-9]{0,4})", binding)
if match is None or int(match.group(1)) > 65535:
    raise SystemExit(f"Unexpected BFF host binding: {binding!r}")
print(match.group(1))
PY
)"
"${PYTHON_BIN}" "${DEADLINE_RUNNER}" \
  --timeout-seconds "${BROWSER_COMMAND_DEADLINE}" \
  --label "run audio import browser E2E" -- \
  env \
  "PYTHON=${PYTHON_BIN}" \
  "AURIS_AUDIO_IMPORT_GATE_BFF_PORT=${BROWSER_BFF_PORT}" \
  "AURIS_AUDIO_IMPORT_GATE_RUN_SUFFIX=${RUN_SUFFIX}" \
  "AURIS_AUDIO_IMPORT_GATE_BROWSER_ARTIFACT=${TEMP_BROWSER_ARTIFACT}" \
  "AURIS_AUDIO_IMPORT_GATE_BROWSER_LOG_DIR=${TEMP_ROOT}/browser-logs" \
  bash "${ROOT}/scripts/verify_audio_import_browser_e2e.sh"
if [ ! -f "${TEMP_BROWSER_ARTIFACT}" ]; then
  echo "Audio import browser E2E did not produce evidence." >&2
  exit 1
fi

"${PYTHON_BIN}" - \
  "${TEMP_BROWSER_ARTIFACT}" \
  "${SOURCE_COMMIT}" \
  "${SOURCE_TREE_DIRTY}" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
artifact = json.loads(path.read_text(encoding="utf-8"))
expected_commit = sys.argv[2]
expected_dirty = sys.argv[3] == "true"
existing_commit = artifact.get("source_commit")
existing_dirty = artifact.get("source_tree_dirty")
if existing_commit not in (None, expected_commit):
    raise SystemExit("Audio import browser evidence source_commit is inconsistent.")
if existing_dirty not in (None, expected_dirty):
    raise SystemExit("Audio import browser evidence source_tree_dirty is inconsistent.")
artifact["source_commit"] = expected_commit
artifact["source_tree_dirty"] = expected_dirty
temporary = path.with_suffix(f"{path.suffix}.tmp")
temporary.write_text(
    json.dumps(artifact, ensure_ascii=True, sort_keys=True, indent=2) + "\n",
    encoding="utf-8",
)
temporary.replace(path)
PY

echo "Exercising HTTPS platform -> Dagster -> MinIO -> signed BFF -> playback..."
compose_with_deadline "${RUN_COMMAND_DEADLINE}" \
  "run audio import real-stack verifier" \
  run --rm --no-deps audio-import-gate-verifier
if [ ! -f "${TEMP_ARTIFACT}" ]; then
  echo "Audio import real-stack verifier did not produce evidence." >&2
  exit 1
fi

"${PYTHON_BIN}" - \
  "${TEMP_ARTIFACT}" \
  "${TEMP_BROWSER_ARTIFACT}" \
  "${SOURCE_COMMIT}" \
  "${SOURCE_TREE_DIRTY}" <<'PY'
import json
import sys
from pathlib import Path

artifact = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
browser_artifact = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
expected_commit = sys.argv[3]
expected_dirty = sys.argv[4] == "true"
if (
    artifact.get("schema_version") != "auris.audio-import-real-stack-gate.v1"
    or artifact.get("status") != "ok"
    or artifact.get("source_commit") != expected_commit
    or artifact.get("source_tree_dirty") is not expected_dirty
    or artifact.get("execution_environment") != "compose"
    or artifact.get("adapters") != {
        "dagster": "real",
        "object_storage": "real",
        "platform_source": "https",
    }
):
    raise SystemExit("Audio import real-stack evidence envelope is invalid.")
if (
    browser_artifact.get("schema_version") != "auris.audio-import-browser-e2e.v2"
    or browser_artifact.get("status") != "ok"
    or browser_artifact.get("source_commit") != expected_commit
    or browser_artifact.get("source_tree_dirty") is not expected_dirty
    or browser_artifact.get("stage") != "completed"
    or browser_artifact.get("mode") != "audio-import-only"
    or browser_artifact.get("executionProfile")
    != {
        "realStack": True,
        "platformSource": "https",
        "inferenceProvider": "https",
        "dagster": "real",
        "objectStorage": "real",
        "uiEvidencePolicy": "browser-clicks-and-bff-readback",
    }
):
    raise SystemExit("Audio import browser evidence envelope is invalid.")
PY
install -m 0644 "${TEMP_ARTIFACT}" "${RESULT_ARTIFACT}"
install -m 0644 "${TEMP_BROWSER_ARTIFACT}" "${BROWSER_RESULT_ARTIFACT}"

if [ "${SOURCE_TREE_DIRTY}" = "true" ]; then
  echo "Warning: evidence is bound to a dirty source tree and is not release evidence." >&2
fi
echo "verify_audio_import_stack ok: ${RESULT_ARTIFACT}"
echo "verify_audio_import_browser_e2e ok: ${BROWSER_RESULT_ARTIFACT}"
