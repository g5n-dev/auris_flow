#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

if [ "$#" -ne 5 ]; then
  echo "Usage: $0 <ghcr.io/...@sha256:digest> <source-commit> <approval-reference> <signature-identity> <signature-issuer>" >&2
  exit 2
fi

ARTIFACT_REF="$1"
SOURCE_COMMIT="$2"
APPROVAL_REFERENCE="$3"
SIGNATURE_IDENTITY="$4"
SIGNATURE_ISSUER="$5"
LOCK_PATH="production/visual/visual-baseline.lock.json"

if [ -n "${PYTHON:-}" ]; then
  PYTHON_BIN="${PYTHON}"
elif [ -x "${ROOT}/backend/.venv/bin/python" ]; then
  PYTHON_BIN="${ROOT}/backend/.venv/bin/python"
else
  PYTHON_BIN="$(command -v python3)"
fi

if [ -n "$(git status --porcelain=v1 --untracked-files=all)" ]; then
  echo "Visual baseline promotion requires a clean source worktree." >&2
  exit 2
fi

"${PYTHON_BIN}" scripts/verify_visual_baseline.py promote-oci \
  --artifact-ref "${ARTIFACT_REF}" \
  --source-commit "${SOURCE_COMMIT}" \
  --approval-reference "${APPROVAL_REFERENCE}" \
  --signature-identity "${SIGNATURE_IDENTITY}" \
  --signature-issuer "${SIGNATURE_ISSUER}" \
  --lock-output "${ROOT}/${LOCK_PATH}"

changed_paths="$(git diff --name-only)"
if [ "${changed_paths}" != "${LOCK_PATH}" ]; then
  echo "Promotion may update only ${LOCK_PATH}; observed:" >&2
  printf '%s\n' "${changed_paths}" >&2
  exit 1
fi

if [ -n "$(git diff --cached --name-only)" ] || [ -n "$(git ls-files --others --exclude-standard)" ]; then
  echo "Promotion created changes outside the unstaged lock pointer." >&2
  exit 1
fi

echo "Visual baseline promotion candidate verified; review and commit only ${LOCK_PATH}."
