#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

if [ "${AURIS_SKIP_REAL_STACK_E2E:-0}" = "1" ]; then
  echo "AURIS_SKIP_REAL_STACK_E2E=1 is not allowed by scripts/verify_release.sh." >&2
  echo "Use bash scripts/verify_fast.sh or AURIS_RUN_E2E=1 bash scripts/verify_all.sh for constrained local development." >&2
  exit 2
fi

AURIS_RELEASE_CHECK=1 AURIS_RUN_E2E=1 bash scripts/verify_all.sh

# Generate deterministic SBOMs and the complete dependency-license inventory
# from the same locked installations used by the release gate. Any unknown or
# unreviewed license fails closed.
python3 scripts/generate_supply_chain_evidence.py

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required for release real-stack E2E." >&2
  echo "Run bash scripts/verify_fast.sh or AURIS_RUN_E2E=1 bash scripts/verify_all.sh for constrained local development." >&2
  exit 2
fi

bash scripts/verify_real_stack.sh
