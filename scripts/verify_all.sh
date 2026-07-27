#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"
export UV_CACHE_DIR="${UV_CACHE_DIR:-${TMPDIR:-/tmp}/auris-flow-uv-cache}"

if [ "${AURIS_RELEASE_CHECK:-0}" = "1" ] && [ "${AURIS_RUN_E2E:-0}" != "1" ]; then
  echo "AURIS_RELEASE_CHECK=1 requires AURIS_RUN_E2E=1." >&2
  exit 2
fi
if [ "${AURIS_RELEASE_CHECK:-0}" = "1" ] && \
  [ "$(git rev-parse --is-shallow-repository)" != "false" ]; then
  echo "Strict release verification requires the full Git history." >&2
  exit 2
fi

bash scripts/verify_static.sh
bash scripts/verify_backend.sh
bash scripts/verify_production_tests.sh
bash scripts/verify_dagster_tests.sh
bash scripts/verify_frontend.sh

if [ "${AURIS_RUN_E2E:-0}" = "1" ]; then
  if [ "${AURIS_RELEASE_CHECK:-0}" = "1" ]; then
    AURIS_E2E_FORCE_AUTOSTART=1 AURIS_REQUIRE_E2E_OUTBOX=1 AURIS_E2E_REQUIRE_COMPLETION_RECEIPTS=1 \
      bash scripts/verify_ui_bff_e2e.sh
  else
    bash scripts/verify_ui_bff_e2e.sh
  fi
  bash scripts/visual_regression.sh
else
  echo "Skipping browser UI/BFF E2E; set AURIS_RUN_E2E=1 to autostart a temp BFF/UI and run the full chain."
fi

if [ "${AURIS_RELEASE_CHECK:-0}" = "1" ]; then
  bash scripts/audit_ui.sh
else
  echo "Skipping strict visual layout audit; the public release gate runs scripts/audit_ui.sh."
fi

if [ "${AURIS_RELEASE_CHECK:-0}" = "1" ]; then
  if [ -n "${PYTHON:-}" ]; then
    PYTHON_BIN="${PYTHON}"
  elif [ -x "${ROOT}/backend/.venv/bin/python" ]; then
    PYTHON_BIN="${ROOT}/backend/.venv/bin/python"
  else
    PYTHON_BIN="$(command -v python3)"
  fi
  release_audit_dir="$(mktemp -d "${TMPDIR:-/tmp}/auris-flow-release-audit.XXXXXX")"
  trap 'rm -rf -- "${release_audit_dir}"' EXIT
  uv export --quiet --frozen --no-dev --no-emit-project --no-header \
    --format requirements.txt --project backend \
    --output-file "${release_audit_dir}/backend-runtime-requirements.txt"
  "${PYTHON_BIN}" -m pip_audit --strict --require-hashes --disable-pip \
    --requirement "${release_audit_dir}/backend-runtime-requirements.txt" \
    --progress-spinner off
  uv export --quiet --frozen --no-dev --no-emit-project --no-header \
    --format requirements.txt --project production/dagster \
    --output-file "${release_audit_dir}/dagster-runtime-requirements.txt"
  "${PYTHON_BIN}" -m pip_audit --strict --require-hashes --disable-pip \
    --requirement "${release_audit_dir}/dagster-runtime-requirements.txt" \
    --progress-spinner off
  npm audit --prefix prototype/auris-flow-ui --audit-level=high
  npm audit signatures --prefix prototype/auris-flow-ui
fi

echo "verify_all ok"
