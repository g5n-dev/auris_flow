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

"${PYTHON_BIN}" - <<'PY'
import importlib.util
import sys

missing = [
    module
    for module in ("fastapi", "sqlalchemy", "pydantic", "pytest", "ruff", "mypy")
    if importlib.util.find_spec(module) is None
]
if missing:
    print("Missing backend dependencies: " + ", ".join(missing), file=sys.stderr)
    print('Run: python3 -m pip install -e "backend[dev]"', file=sys.stderr)
    sys.exit(2)
PY

"${PYTHON_BIN}" doc/backend-spec/validate_backend_spec.py
"${PYTHON_BIN}" scripts/validate_public_audio_datasets.py
"${PYTHON_BIN}" scripts/verify_production_compose.py
"${PYTHON_BIN}" scripts/verify_github_actions_pins.py
"${PYTHON_BIN}" -m unittest discover -s scripts/tests -p 'test_*.py'

if [ "${AURIS_RELEASE_CHECK:-0}" = "1" ]; then
  if ! command -v uv >/dev/null 2>&1; then
    echo "Strict release verification requires uv." >&2
    exit 2
  fi
  uv lock --check --project backend
  uv sync --check --locked --all-extras --project backend
  uv lock --check --project production/dagster
  uv sync --check --locked --all-extras --project production/dagster
  "${PYTHON_BIN}" scripts/scan_secrets.py --history
  "${PYTHON_BIN}" scripts/check_platform_readiness.py --release
else
  "${PYTHON_BIN}" scripts/scan_secrets.py
  "${PYTHON_BIN}" scripts/check_platform_readiness.py
fi

"${PYTHON_BIN}" -m ruff format --check backend scripts production/tests
"${PYTHON_BIN}" -m ruff check backend scripts production/tests
"${PYTHON_BIN}" -m mypy \
  backend/app \
  backend/scripts/verify_migrations.py \
  scripts/check_platform_readiness.py \
  scripts/finalize_release_evidence.py \
  scripts/generate_supply_chain_evidence.py \
  scripts/verify_real_dagster.py
MYPYPATH=backend "${PYTHON_BIN}" -m mypy \
  scripts/verify_product_dagster_path.py \
  scripts/verify_license_materials.py \
  scripts/verify_visual_baseline.py

echo "verify_static ok"
