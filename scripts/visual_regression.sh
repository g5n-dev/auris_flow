#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UI_ROOT="${ROOT}/prototype/auris-flow-ui"
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

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/auris-visual-regression.XXXXXX")"
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

LOCK_PATH="${ROOT}/production/visual/visual-baseline.lock.json"
CANONICAL_SEED_OVERLAY="${ROOT}/production/visual/seed-overlay.json"
LOCKED_BASELINE_ROOT="${TMP_DIR}/locked-baseline"
LOCKED_VISUAL_DIR="${LOCKED_BASELINE_ROOT}/visual-regression"
DIAGNOSTICS_ROOT="${UI_ROOT}/e2e/artifacts"
VISUAL_EVIDENCE_PATH="${ROOT}/build/release-evidence/visual-regression.json"
UPDATE_BASELINE="${AURIS_UPDATE_VISUAL_BASELINE:-0}"
RELEASE_CHECK="${AURIS_RELEASE_CHECK:-0}"
VISUAL_RUNTIME_REQUESTED="${AURIS_VISUAL_RUNTIME:-container}"
POLICY_ARGS=(
  --release-check "${RELEASE_CHECK}"
  --update "${UPDATE_BASELINE}"
  --default-goal-dir "${LOCKED_BASELINE_ROOT}"
  --frozen-root "${LOCKED_BASELINE_ROOT}"
  --diagnostics-root "${DIAGNOSTICS_ROOT}"
  --runtime "${VISUAL_RUNTIME_REQUESTED}"
)
if [ -n "${AURIS_VISUAL_GOAL_DIR:-}" ]; then
  POLICY_ARGS+=(--goal-dir "${AURIS_VISUAL_GOAL_DIR}")
fi
if [ -n "${AURIS_VISUAL_SEED_OVERLAY:-}" ]; then
  POLICY_ARGS+=(--seed-overlay "${AURIS_VISUAL_SEED_OVERLAY}")
fi
POLICY_JSON="$(
  "${PYTHON_BIN}" scripts/verify_visual_baseline.py check-execution-policy \
    "${POLICY_ARGS[@]}"
)"
VISUAL_DIR="$(
  "${PYTHON_BIN}" -c 'import json,sys; print(json.loads(sys.argv[1])["visual_dir"])' \
    "${POLICY_JSON}"
)"
VISUAL_RUNTIME="$(
  "${PYTHON_BIN}" -c 'import json,sys; print(json.loads(sys.argv[1])["runtime"])' \
    "${POLICY_JSON}"
)"

if [ "${RELEASE_CHECK}" = "1" ]; then
  rm -f "${VISUAL_EVIDENCE_PATH}"
fi

if [ "${UPDATE_BASELINE}" = "1" ]; then
  mkdir -p "${VISUAL_DIR}/screenshots"
else
  MATERIALIZE_ARGS=(
    --lock "${LOCK_PATH}"
    --destination "${LOCKED_VISUAL_DIR}"
  )
  if [ "${RELEASE_CHECK}" = "1" ]; then
    MATERIALIZE_ARGS+=(--verify-signature)
  fi
  "${PYTHON_BIN}" scripts/verify_visual_baseline.py materialize-locked \
    "${MATERIALIZE_ARGS[@]}"
fi

if [ ! -d "${UI_ROOT}/node_modules" ]; then
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
  local log_file="$3"
  for _ in $(seq 1 100); do
    if curl -fsS "${url}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.25
  done
  echo "${label} did not become ready: ${url}" >&2
  tail -80 "${log_file}" >&2 || true
  return 1
}

BFF_PORT="${AURIS_VISUAL_BFF_PORT:-$(free_port)}"
UI_PORT="${AURIS_VISUAL_UI_PORT:-$(free_port)}"
BFF_URL="http://127.0.0.1:${BFF_PORT}"
UI_URL="http://127.0.0.1:${UI_PORT}/"
VISUAL_SEED_SOURCE="${AURIS_VISUAL_SEED_OVERLAY:-${CANONICAL_SEED_OVERLAY}}"
VISUAL_SEED_OVERLAY="${VISUAL_DIR}/seed-overlay.json"
if [ "${UPDATE_BASELINE}" = "1" ]; then
  if [ ! -f "${VISUAL_SEED_SOURCE}" ]; then
    echo "Visual seed overlay is missing: ${VISUAL_SEED_SOURCE}" >&2
    exit 2
  fi
  if [ ! -e "${VISUAL_SEED_OVERLAY}" ] || [ ! "${VISUAL_SEED_SOURCE}" -ef "${VISUAL_SEED_OVERLAY}" ]; then
    cp "${VISUAL_SEED_SOURCE}" "${VISUAL_SEED_OVERLAY}"
  fi
fi

if [ ! -f "${VISUAL_SEED_OVERLAY}" ]; then
  echo "Visual seed overlay is missing: ${VISUAL_SEED_OVERLAY}" >&2
  exit 2
fi

DB_URL="sqlite:///${TMP_DIR}/auris_visual.sqlite"
RUNTIME_DESCRIPTOR="${TMP_DIR}/visual-runtime.json"
CONTAINER_ARTIFACT_DIR="${TMP_DIR}/container-artifacts"
RUNNER_IMAGE_TAG=""
CONTAINER_UI_URL=""
ALLOW_DOCKER_HOST_PREVIEW="0"
DOCKER_NETWORK_ARGS=()

if [ "${VISUAL_RUNTIME}" = "container" ]; then
  if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
    echo "Docker Engine is required for pinned visual verification." >&2
    exit 2
  fi
  case "$(uname -s)" in
    Linux)
      DOCKER_NETWORK_ARGS=(--network host)
      CONTAINER_UI_URL="${UI_URL}"
      ;;
    Darwin)
      DOCKER_NETWORK_ARGS=(--add-host host.docker.internal:host-gateway)
      CONTAINER_UI_URL="http://host.docker.internal:${UI_PORT}/"
      ALLOW_DOCKER_HOST_PREVIEW="1"
      ;;
    *)
      echo "Pinned visual verification supports Linux and macOS Docker hosts only." >&2
      exit 2
      ;;
  esac

  RUNNER_CONTRACT_SHA256="$(
    "${PYTHON_BIN}" scripts/verify_visual_baseline.py runner-contract-sha256
  )"
  RUNNER_IMAGE_TAG="auris-flow-visual-runner:${RUNNER_CONTRACT_SHA256:0:20}"
  echo "Building pinned linux/amd64 Playwright runner (${RUNNER_IMAGE_TAG})..."
  docker build \
    --platform linux/amd64 \
    --file "${ROOT}/production/visual/Dockerfile" \
    --build-arg "AURIS_VISUAL_RUNNER_CONTRACT_SHA256=${RUNNER_CONTRACT_SHA256}" \
    --tag "${RUNNER_IMAGE_TAG}" \
    "${ROOT}" >/dev/null

  mkdir -p "${CONTAINER_ARTIFACT_DIR}"
  docker run --rm \
    --platform linux/amd64 \
    --init \
    --read-only \
    --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --user "$(id -u):$(id -g)" \
    --env HOME=/tmp/auris-home \
    --tmpfs /tmp:rw,exec,nosuid,size=1g \
    --ipc=host \
    --entrypoint node \
    "${RUNNER_IMAGE_TAG}" /opt/auris-visual/runtime.mjs >"${RUNTIME_DESCRIPTOR}"

  if [ "${UPDATE_BASELINE}" != "1" ]; then
    "${PYTHON_BIN}" scripts/verify_visual_baseline.py verify \
      "${VISUAL_DIR}" \
      --runtime-descriptor "${RUNTIME_DESCRIPTOR}" \
      --require-release-runtime
  fi
fi

(
  cd backend
  DATABASE_URL="${DB_URL}" "${PYTHON_BIN}" -m alembic upgrade head >/dev/null
  DATABASE_URL="${DB_URL}" AURIS_VISUAL_SEED_OVERLAY="${VISUAL_SEED_OVERLAY}" "${PYTHON_BIN}" - <<'PY' >/dev/null
import hashlib
import json
import os
from pathlib import Path

from app.core.database import SessionLocal
from app.schemas.scene_profiles import SceneProfileManifest
from app.services.resource_service import load_seed_file, seed_database

seed = load_seed_file()
overlay = json.loads(Path(os.environ["AURIS_VISUAL_SEED_OVERLAY"]).read_text(encoding="utf-8"))
versions = seed.get("scene_profiles", {}).get("versions", [])

for override in overlay.get("scene_profile_versions", []):
    version_id = override["scene_profile_version_id"]
    matches = [item for item in versions if item.get("scene_profile_version_id") == version_id]
    if len(matches) != 1:
        raise SystemExit(f"visual seed overlay expected exactly one {version_id}, found {len(matches)}")
    item = matches[0]
    item["manifest"] = {**item["manifest"], **override.get("manifest", {})}
    manifest = SceneProfileManifest.model_validate(item["manifest"]).model_dump(mode="json")
    manifest_sha256 = hashlib.sha256(
        json.dumps(manifest, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if manifest_sha256 != override["expected_manifest_sha256"]:
        raise SystemExit(
            f"visual seed manifest drift for {version_id}: "
            f"expected {override['expected_manifest_sha256']}, got {manifest_sha256}"
        )
    item["manifest"] = manifest

with SessionLocal() as session:
    seed_database(session, seed)
PY
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

npm --prefix "${UI_ROOT}" run build >/dev/null
(
  cd "${UI_ROOT}"
  AURIS_ALLOW_DOCKER_HOST_PREVIEW="${ALLOW_DOCKER_HOST_PREVIEW}" \
    VITE_API_PROXY_TARGET="${BFF_URL}" npm exec vite -- preview \
    --host 127.0.0.1 --port "${UI_PORT}" --strictPort >"${TMP_DIR}/preview.log" 2>&1
) &
UI_PID=$!
wait_for_url "${UI_URL}" "UI preview" "${TMP_DIR}/preview.log"
wait_for_url "${UI_URL%/}/healthz" "UI proxy" "${TMP_DIR}/preview.log"

UPDATE_MODE="none"
if [ "${UPDATE_BASELINE}" = "1" ]; then
  UPDATE_MODE="all"
fi

if [ "${VISUAL_RUNTIME}" = "container" ]; then
  BASELINE_MOUNT_MODE="ro"
  if [ "${UPDATE_BASELINE}" = "1" ]; then
    BASELINE_MOUNT_MODE="rw"
  fi
  # Playwright clears outputDir before a run. Keep it below the bind-mount root;
  # removing the mount point itself fails under the container's read-only rootfs.
  docker run --rm \
    --platform linux/amd64 \
    --init \
    --read-only \
    --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --user "$(id -u):$(id -g)" \
    --env HOME=/tmp/auris-home \
    --env TZ=Asia/Shanghai \
    --env LANG=zh_CN.UTF-8 \
    --env LC_ALL=zh_CN.UTF-8 \
    --env "AURIS_AUDIT_URL=${CONTAINER_UI_URL}" \
    --env AURIS_VISUAL_BASELINE_DIR=/baseline/screenshots \
    --env AURIS_VISUAL_GEOMETRY_PATH=/baseline/geometry.json \
    --env AURIS_VISUAL_ARTIFACT_DIR=/artifacts/test-results \
    --env "AURIS_UPDATE_VISUAL_BASELINE=${UPDATE_BASELINE}" \
    --tmpfs /tmp:rw,exec,nosuid,size=1g \
    --ipc=host \
    "${DOCKER_NETWORK_ARGS[@]}" \
    --volume "${VISUAL_DIR}:/baseline:${BASELINE_MOUNT_MODE}" \
    --volume "${CONTAINER_ARTIFACT_DIR}:/artifacts:rw" \
    "${RUNNER_IMAGE_TAG}" --update-snapshots="${UPDATE_MODE}"
else
  env \
    TZ=Asia/Shanghai \
    LANG=zh_CN.UTF-8 \
    LC_ALL=zh_CN.UTF-8 \
    AURIS_AUDIT_URL="${UI_URL}" \
    AURIS_VISUAL_BASELINE_DIR="${VISUAL_DIR}/screenshots" \
    AURIS_VISUAL_GEOMETRY_PATH="${VISUAL_DIR}/geometry.json" \
    AURIS_UPDATE_VISUAL_BASELINE="${UPDATE_BASELINE}" \
    npm --prefix "${UI_ROOT}" run audit:visual -- --update-snapshots="${UPDATE_MODE}"
fi

if [ "${UPDATE_BASELINE}" = "1" ]; then
  if [ "${VISUAL_RUNTIME}" = "container" ]; then
    "${PYTHON_BIN}" scripts/verify_visual_baseline.py write-manifest \
      "${VISUAL_DIR}" --runtime-descriptor "${RUNTIME_DESCRIPTOR}"
  else
    "${PYTHON_BIN}" scripts/verify_visual_baseline.py write-manifest "${VISUAL_DIR}"
  fi
else
  "${PYTHON_BIN}" scripts/verify_visual_baseline.py verify \
    "${VISUAL_DIR}" \
    --runtime-descriptor "${RUNTIME_DESCRIPTOR}" \
    --require-release-runtime
  if [ "${RELEASE_CHECK}" = "1" ]; then
    "${PYTHON_BIN}" scripts/verify_visual_baseline.py write-evidence \
      "${VISUAL_DIR}" \
      --lock "${LOCK_PATH}" \
      --runtime-descriptor "${RUNTIME_DESCRIPTOR}" \
      --output "${VISUAL_EVIDENCE_PATH}"
  fi
fi

echo "visual_regression ok: ${VISUAL_DIR}"
