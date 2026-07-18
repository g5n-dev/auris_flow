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

if [ ! -d "${UI_ROOT}/node_modules" ]; then
  echo "Frontend dependencies missing. Run: npm ci --prefix prototype/auris-flow-ui" >&2
  exit 2
fi

BASELINE_POINTER="${UI_ROOT}/audit-iteration/frontend-decomposition/current-baseline.txt"
if [ -n "${AURIS_VISUAL_GOAL_DIR:-}" ]; then
  GOAL_DIR="${AURIS_VISUAL_GOAL_DIR}"
elif [ -f "${BASELINE_POINTER}" ]; then
  GOAL_DIR="$(cat "${BASELINE_POINTER}")"
else
  echo "Visual Goal baseline pointer is missing: ${BASELINE_POINTER}" >&2
  exit 2
fi

if [ "${AURIS_UPDATE_VISUAL_BASELINE:-0}" = "1" ] && [ -z "${AURIS_VISUAL_GOAL_DIR:-}" ] && [ "${AURIS_ALLOW_UPDATE_FROZEN_BASELINE:-0}" != "1" ]; then
  echo "Refusing to overwrite the frozen visual baseline. Set AURIS_VISUAL_GOAL_DIR to a separate diagnostics directory." >&2
  exit 2
fi
VISUAL_DIR="${GOAL_DIR}/visual-regression"
mkdir -p "${VISUAL_DIR}/screenshots"

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

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/auris-visual-regression.XXXXXX")"
BFF_PORT="${AURIS_VISUAL_BFF_PORT:-$(free_port)}"
UI_PORT="${AURIS_VISUAL_UI_PORT:-$(free_port)}"
DB_URL="sqlite:///${TMP_DIR}/auris_visual.sqlite"
BFF_URL="http://127.0.0.1:${BFF_PORT}"
UI_URL="http://127.0.0.1:${UI_PORT}/"
VISUAL_SEED_OVERLAY="${AURIS_VISUAL_SEED_OVERLAY:-${UI_ROOT}/audit/visual-phase0-seed-overlay.json}"
BFF_PID=""
UI_PID=""

if [ ! -f "${VISUAL_SEED_OVERLAY}" ]; then
  echo "Visual seed overlay is missing: ${VISUAL_SEED_OVERLAY}" >&2
  exit 2
fi

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
  VITE_API_PROXY_TARGET="${BFF_URL}" npm exec vite -- preview \
    --host 127.0.0.1 --port "${UI_PORT}" --strictPort >"${TMP_DIR}/preview.log" 2>&1
) &
UI_PID=$!
wait_for_url "${UI_URL}" "UI preview" "${TMP_DIR}/preview.log"
wait_for_url "${UI_URL%/}/healthz" "UI proxy" "${TMP_DIR}/preview.log"

UPDATE_MODE="none"
if [ "${AURIS_UPDATE_VISUAL_BASELINE:-0}" = "1" ]; then
  UPDATE_MODE="all"
fi

env \
  TZ=Asia/Shanghai \
  LANG=zh_CN.UTF-8 \
  LC_ALL=zh_CN.UTF-8 \
  AURIS_AUDIT_URL="${UI_URL}" \
  AURIS_VISUAL_BASELINE_DIR="${VISUAL_DIR}/screenshots" \
  AURIS_VISUAL_GEOMETRY_PATH="${VISUAL_DIR}/geometry.json" \
  AURIS_UPDATE_VISUAL_BASELINE="${AURIS_UPDATE_VISUAL_BASELINE:-0}" \
  npm --prefix "${UI_ROOT}" run audit:visual -- --update-snapshots="${UPDATE_MODE}"

echo "visual_regression ok: ${VISUAL_DIR}"
