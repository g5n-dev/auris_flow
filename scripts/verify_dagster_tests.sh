#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

if ! command -v uv >/dev/null 2>&1; then
  echo "Dagster verification requires uv." >&2
  exit 2
fi

if [ -n "${AURIS_JUNIT_DIR:-}" ]; then
  mkdir -p "${AURIS_JUNIT_DIR}"
  uv run --frozen --all-extras --project production/dagster \
    pytest production/dagster/tests \
    --junitxml="${AURIS_JUNIT_DIR}/dagster.xml"
else
  uv run --frozen --all-extras --project production/dagster \
    pytest production/dagster/tests
fi
uv run --frozen --all-extras --project production/dagster \
  ruff format --check production/dagster/src production/dagster/tests
uv run --frozen --all-extras --project production/dagster \
  ruff check production/dagster/src production/dagster/tests
uv run --frozen --all-extras --project production/dagster \
  mypy production/dagster/src

echo "verify_dagster_tests ok"
