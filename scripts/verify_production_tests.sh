#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

if [ -n "${PYTHON:-}" ]; then
  PYTHON_BIN="${PYTHON}"
elif [ -x "${ROOT}/backend/.venv/bin/python" ]; then
  PYTHON_BIN="${ROOT}/backend/.venv/bin/python"
else
  PYTHON_BIN="$(command -v python3)"
fi

if [ -n "${AURIS_JUNIT_DIR:-}" ]; then
  mkdir -p "${AURIS_JUNIT_DIR}"
  "${PYTHON_BIN}" -m pytest production/tests \
    --junitxml="${AURIS_JUNIT_DIR}/production.xml"
else
  "${PYTHON_BIN}" -m pytest production/tests
fi

echo "verify_production_tests ok"
