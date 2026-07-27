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
fi

"${PYTHON_BIN}" backend/scripts/verify_migrations.py
if [ -n "${AURIS_JUNIT_DIR:-}" ]; then
  "${PYTHON_BIN}" -m pytest \
    backend/tests/unit backend/tests/contract backend/tests/integration \
    --junitxml="${AURIS_JUNIT_DIR}/backend.xml"
else
  "${PYTHON_BIN}" -m pytest \
    backend/tests/unit backend/tests/contract backend/tests/integration
fi
"${PYTHON_BIN}" backend/scripts/smoke_backend.py

echo "verify_backend ok"
