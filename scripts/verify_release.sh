#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"
BUILD_DIR="${ROOT}/build"
EVIDENCE_REL="build/release-evidence"
EVIDENCE_DIR="${ROOT}/${EVIDENCE_REL}"

if [ "${AURIS_SKIP_REAL_STACK_E2E:-0}" = "1" ]; then
  echo "AURIS_SKIP_REAL_STACK_E2E=1 is not allowed by scripts/verify_release.sh." >&2
  echo "Use bash scripts/verify_fast.sh or AURIS_RUN_E2E=1 bash scripts/verify_all.sh for constrained local development." >&2
  exit 2
fi
if [ "${AURIS_SKIP_REAL_DAGSTER:-0}" = "1" ]; then
  echo "AURIS_SKIP_REAL_DAGSTER=1 is not allowed by scripts/verify_release.sh." >&2
  echo "Use bash scripts/verify_fast.sh for constrained local development; it is not release evidence." >&2
  exit 2
fi
if [ "${AURIS_SKIP_PRODUCT_DAGSTER_GATE:-0}" = "1" ]; then
  echo "AURIS_SKIP_PRODUCT_DAGSTER_GATE=1 is not allowed by scripts/verify_release.sh." >&2
  echo "The product-path BFF/Worker/real-Dagster proof is mandatory release evidence." >&2
  exit 2
fi
if [ "${AURIS_SKIP_PRODUCTION_PATH_GATE:-0}" = "1" ]; then
  echo "AURIS_SKIP_PRODUCTION_PATH_GATE=1 is not allowed by scripts/verify_release.sh." >&2
  echo "The single production Compose path is a mandatory fail-closed release gate." >&2
  exit 2
fi

if [ -L "${ROOT}/build" ]; then
  echo "Release build path must be a real directory; symlinks are forbidden: build" >&2
  exit 2
fi
if [ -e "${BUILD_DIR}" ] && [ ! -d "${BUILD_DIR}" ]; then
  echo "Release build path must be a real directory: build" >&2
  exit 2
fi
if [ -L "${EVIDENCE_DIR}" ]; then
  echo "Release evidence path must be a real directory; symlinks are forbidden: build/release-evidence" >&2
  exit 2
fi
if [ -e "${EVIDENCE_DIR}" ] && [ ! -d "${EVIDENCE_DIR}" ]; then
  echo "Release evidence path must be a real directory: build/release-evidence" >&2
  exit 2
fi

# Create one path component at a time after lstat-style shell guards. Avoid
# `mkdir -p`, which could silently traverse a symlinked parent.
if [ ! -d "${BUILD_DIR}" ]; then
  mkdir -- "${BUILD_DIR}"
fi
if [ -L "${BUILD_DIR}" ] || [ ! -d "${BUILD_DIR}" ]; then
  echo "Release build path must be a real directory after creation: build" >&2
  exit 2
fi
if [ ! -d "${EVIDENCE_DIR}" ]; then
  mkdir -- "${EVIDENCE_DIR}"
fi
if [ -L "${EVIDENCE_DIR}" ] || [ ! -d "${EVIDENCE_DIR}" ]; then
  echo "Release evidence path must be a real directory after creation: build/release-evidence" >&2
  exit 2
fi

SOURCE_COMMIT="$(git rev-parse --verify HEAD^{commit})"
source_status="$(git status --porcelain=v1 --untracked-files=all)"
if [ -n "${source_status}" ]; then
  echo "Release gate requires a clean committed HEAD; staged, unstaged, and untracked source are forbidden." >&2
  printf '%s\n' "${source_status}" >&2
  exit 2
fi
if find "${EVIDENCE_DIR}" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
  echo "Release evidence directory must be empty before a new gate run: build/release-evidence" >&2
  echo "Move prior diagnostics aside; stale evidence is never reused or silently deleted." >&2
  exit 2
fi

if [ -n "${PYTHON:-}" ]; then
  PYTHON_BIN="${PYTHON}"
elif [ -x "${ROOT}/backend/.venv/bin/python" ]; then
  PYTHON_BIN="${ROOT}/backend/.venv/bin/python"
else
  PYTHON_BIN="$(command -v python3 || true)"
fi
if [ ! -x "${PYTHON_BIN}" ]; then
  echo "Release Python is not executable; set PYTHON to an absolute environment path." >&2
  exit 2
fi

"${PYTHON_BIN}" scripts/verify_release_authorization.py
AURIS_RELEASE_CHECK=1 bash scripts/verify_clean_clone.sh
AURIS_RELEASE_CHECK=1 AURIS_RUN_E2E=1 bash scripts/verify_all.sh

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required for release real-stack and real Dagster E2E." >&2
  echo "Run bash scripts/verify_fast.sh or AURIS_RUN_E2E=1 bash scripts/verify_all.sh for constrained local development." >&2
  exit 2
fi

bash scripts/verify_production_mysql_migrations.sh
bash scripts/verify_real_stack.sh
bash scripts/verify_real_dagster.sh
bash scripts/verify_product_dagster_path.sh
bash scripts/verify_production_path.sh

# Generate deterministic SBOMs and the complete dependency-license inventory
# only after runtime gates have succeeded. The final manifest below hashes every
# accepted artifact and binds it to this exact clean commit.
"${PYTHON_BIN}" scripts/generate_supply_chain_evidence.py \
  --source-commit "${SOURCE_COMMIT}"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required to export the locked Python runtime graphs for release audit." >&2
  exit 2
fi
if ! command -v npm >/dev/null 2>&1; then
  echo "npm is required to audit the locked frontend runtime graph." >&2
  exit 2
fi
if ! "${PYTHON_BIN}" -m pip_audit --version >/dev/null 2>&1; then
  echo "pip-audit is required in the selected release Python environment." >&2
  exit 2
fi

uv export --quiet --frozen --no-dev --no-emit-project --no-header \
  --format requirements.txt \
  --project backend \
  --output-file "${EVIDENCE_REL}/backend-runtime-requirements.txt"
uv export --quiet --frozen --no-dev --no-emit-project --no-header \
  --format requirements.txt \
  --project production/dagster \
  --output-file "${EVIDENCE_REL}/dagster-runtime-requirements.txt"

# Run every audit before returning failure so CI retains a complete diagnostic set.
# A failed audit never reaches the finalizer, therefore no pre-audit success manifest
# can exist or be uploaded as successful release evidence.
audit_status=0
"${PYTHON_BIN}" -m pip_audit --strict --require-hashes --disable-pip \
  --requirement "${EVIDENCE_REL}/backend-runtime-requirements.txt" \
  --format json \
  --output "${EVIDENCE_REL}/backend-python-audit.json" \
  || audit_status=1
"${PYTHON_BIN}" -m pip_audit --strict --require-hashes --disable-pip \
  --requirement "${EVIDENCE_REL}/dagster-runtime-requirements.txt" \
  --format json \
  --output "${EVIDENCE_REL}/dagster-python-audit.json" \
  || audit_status=1
npm audit --prefix prototype/auris-flow-ui --audit-level=high --json \
  > "${EVIDENCE_REL}/npm-audit.json" \
  || audit_status=1
if [ "${audit_status}" -ne 0 ]; then
  echo "Release dependency audit failed; no final release evidence manifest was created." >&2
  exit "${audit_status}"
fi

"${PYTHON_BIN}" scripts/finalize_release_evidence.py \
  --source-commit "${SOURCE_COMMIT}" \
  --require-audits
