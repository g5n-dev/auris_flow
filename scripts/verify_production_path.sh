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
GATE_COMPOSE="${ROOT}/production/tests/production-path-gate.compose.yaml"
RUNTIME_DRIVER="${ROOT}/scripts/verify_production_path_runtime.py"
VALIDATOR="${ROOT}/scripts/verify_production_path_gate.py"
BUILD_DIR="${ROOT}/build"
EVIDENCE_DIR="${BUILD_DIR}/release-evidence"
# Canonical release artifact: build/release-evidence/production-path-gate.json
ARTIFACT="${EVIDENCE_DIR}/production-path-gate.json"

if [ "${AURIS_SKIP_PRODUCTION_PATH_GATE:-0}" = "1" ]; then
  echo "AURIS_SKIP_PRODUCTION_PATH_GATE=1 is not allowed by the production path gate." >&2
  exit 2
fi

if [ -L "${ROOT}/build" ]; then
  echo "Production path build path must be a real directory; symlinks are forbidden: build" >&2
  exit 2
fi
if [ -e "${BUILD_DIR}" ] && [ ! -d "${BUILD_DIR}" ]; then
  echo "Production path build path must be a real directory: build" >&2
  exit 2
fi
if [ -L "${EVIDENCE_DIR}" ]; then
  echo "Production path evidence path must be a real directory; symlinks are forbidden: build/release-evidence" >&2
  exit 2
fi
if [ -e "${EVIDENCE_DIR}" ] && [ ! -d "${EVIDENCE_DIR}" ]; then
  echo "Production path evidence path must be a real directory: build/release-evidence" >&2
  exit 2
fi
if [ -L "${ARTIFACT}" ]; then
  echo "Production path evidence artifact must not be a symlink." >&2
  exit 2
fi
if [ -e "${ARTIFACT}" ]; then
  echo "Production path evidence artifact already exists; stale evidence is never reused or deleted." >&2
  exit 2
fi

# Preflight runs before Docker and source-tree checks so the checked-in blocked
# contract reports its exact missing capabilities.  A successful preflight is
# explicitly not release evidence.
"${PYTHON_BIN}" "${VALIDATOR}" preflight \
  --compose "production/tests/production-path-gate.compose.yaml"

if [ ! -d "${BUILD_DIR}" ]; then
  mkdir -- "${BUILD_DIR}"
fi
if [ -L "${BUILD_DIR}" ] || [ ! -d "${BUILD_DIR}" ]; then
  echo "Production path build path must be a real directory after creation: build" >&2
  exit 2
fi
if [ ! -d "${EVIDENCE_DIR}" ]; then
  mkdir -- "${EVIDENCE_DIR}"
fi
if [ -L "${EVIDENCE_DIR}" ] || [ ! -d "${EVIDENCE_DIR}" ]; then
  echo "Production path evidence path must be a real directory after creation: build/release-evidence" >&2
  exit 2
fi

if [ ! -f "${RUNTIME_DRIVER}" ]; then
  echo "Production path runtime driver is not implemented; preflight cannot become runtime evidence." >&2
  exit 2
fi
"${PYTHON_BIN}" "${ROOT}/scripts/verify_production_compose.py"
if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  echo "Docker Engine is required for the production path gate." >&2
  exit 2
fi
if ! git -C "${ROOT}" diff --quiet -- || ! git -C "${ROOT}" diff --cached --quiet --; then
  echo "Production path gate requires a clean worktree and empty Git index." >&2
  exit 2
fi
if [ -n "$(git -C "${ROOT}" ls-files --others --exclude-standard)" ]; then
  echo "Production path gate refuses untracked source inputs." >&2
  exit 2
fi
SOURCE_COMMIT="$(git -C "${ROOT}" rev-parse --verify HEAD^{commit})"

"${PYTHON_BIN}" "${RUNTIME_DRIVER}" \
  --base-compose "${BASE_COMPOSE}" \
  --gate-compose "${GATE_COMPOSE}" \
  --source-commit "${SOURCE_COMMIT}" \
  --artifact "${ARTIFACT}"
"${PYTHON_BIN}" "${VALIDATOR}" evidence \
  --artifact "${ARTIFACT}" \
  --expected-commit "${SOURCE_COMMIT}"
