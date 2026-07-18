#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"
export UV_CACHE_DIR="${UV_CACHE_DIR:-${TMPDIR:-/tmp}/auris-flow-uv-cache}"

if [ -n "${PYTHON:-}" ]; then
  PYTHON_BIN="${PYTHON}"
elif [ -x "${ROOT}/backend/.venv/bin/python" ]; then
  PYTHON_BIN="${ROOT}/backend/.venv/bin/python"
else
  PYTHON_BIN="$(command -v python3)"
fi

echo "Using Python: ${PYTHON_BIN}"

"${PYTHON_BIN}" - <<'PY'
import importlib.util
import sys

missing = [
    module
    for module in ("fastapi", "sqlalchemy", "pydantic", "pytest", "httpx", "ruff", "mypy")
    if importlib.util.find_spec(module) is None
]
if missing:
    print("Missing backend dependencies: " + ", ".join(missing), file=sys.stderr)
    print('Run: python3 -m pip install -e "backend[dev]"', file=sys.stderr)
    print("Or set PYTHON=/path/to/backend/.venv/bin/python before running this script.", file=sys.stderr)
    sys.exit(2)
PY

"${PYTHON_BIN}" doc/backend-spec/validate_backend_spec.py
"${PYTHON_BIN}" scripts/validate_public_audio_datasets.py
"${PYTHON_BIN}" scripts/check_platform_readiness.py
"${PYTHON_BIN}" -m unittest discover -s scripts/tests -p 'test_*.py'

if [ "${AURIS_RELEASE_CHECK:-0}" = "1" ]; then
  if [ "${AURIS_RUN_E2E:-0}" != "1" ]; then
    echo "AURIS_RELEASE_CHECK=1 requires AURIS_RUN_E2E=1 so browser UI/BFF E2E and failed-response gates are enforced." >&2
    echo "Run: AURIS_RELEASE_CHECK=1 AURIS_RUN_E2E=1 bash scripts/verify_all.sh" >&2
    exit 2
  fi
  if [ "$(git rev-parse --is-shallow-repository)" != "false" ]; then
    echo "Strict release verification requires the full Git history (fetch-depth: 0)." >&2
    exit 2
  fi
  if ! command -v uv >/dev/null 2>&1; then
    echo "Strict release verification requires uv so backend/uv.lock is authoritative." >&2
    exit 2
  fi
  uv lock --check --project backend
  uv sync --check --locked --all-extras --project backend
  uv lock --check --project production/dagster
  uv sync --check --locked --all-extras --project production/dagster
  "${PYTHON_BIN}" scripts/verify_production_compose.py
  "${PYTHON_BIN}" -m pytest production/tests
  uv run --frozen --all-extras --project production/dagster pytest production/dagster/tests
  uv run --frozen --all-extras --project production/dagster ruff format --check production/dagster/src production/dagster/tests
  uv run --frozen --all-extras --project production/dagster ruff check production/dagster/src production/dagster/tests
  uv run --frozen --all-extras --project production/dagster mypy production/dagster/src
  "${PYTHON_BIN}" scripts/scan_secrets.py --history
  "${PYTHON_BIN}" scripts/check_platform_readiness.py --release
  "${PYTHON_BIN}" -m pip_audit --local --strict --skip-editable --progress-spinner off
  release_audit_dir="$(mktemp -d "${TMPDIR:-/tmp}/auris-flow-release-audit.XXXXXX")"
  trap 'rm -rf -- "${release_audit_dir}"' EXIT
  uv export --quiet --frozen --no-dev --no-emit-project \
    --format requirements.txt --project production/dagster \
    --output-file "${release_audit_dir}/dagster-runtime-requirements.txt"
  "${PYTHON_BIN}" -m pip_audit --strict --require-hashes --disable-pip \
    --requirement "${release_audit_dir}/dagster-runtime-requirements.txt" \
    --progress-spinner off
  npm audit --prefix prototype/auris-flow-ui --audit-level=high
  npm audit signatures --prefix prototype/auris-flow-ui
else
  "${PYTHON_BIN}" scripts/scan_secrets.py
  echo "Skipping strict open-source release check; set AURIS_RELEASE_CHECK=1 for public release gates."
fi

if [ ! -d backend ]; then
  echo "backend/ missing"
  exit 1
fi

"${PYTHON_BIN}" -m ruff format --check backend scripts production/tests
"${PYTHON_BIN}" -m ruff check backend scripts production/tests
"${PYTHON_BIN}" -m mypy backend/app backend/scripts/verify_migrations.py scripts/check_platform_readiness.py scripts/finalize_release_evidence.py scripts/generate_supply_chain_evidence.py scripts/verify_real_dagster.py
MYPYPATH=backend "${PYTHON_BIN}" -m mypy \
  scripts/verify_product_dagster_path.py \
  scripts/verify_release_authorization.py \
  scripts/verify_visual_baseline.py
"${PYTHON_BIN}" backend/scripts/verify_migrations.py
"${PYTHON_BIN}" -m pytest backend/tests/unit backend/tests/contract backend/tests/integration
"${PYTHON_BIN}" backend/scripts/smoke_backend.py
npm --prefix prototype/auris-flow-ui run architecture:test
npm --prefix prototype/auris-flow-ui run architecture:final
npm --prefix prototype/auris-flow-ui run build
npm --prefix prototype/auris-flow-ui run bundle:check
npm --prefix prototype/auris-flow-ui run e2e:preview:check
npm --prefix prototype/auris-flow-ui run e2e:ui

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

echo "verify_all ok"
