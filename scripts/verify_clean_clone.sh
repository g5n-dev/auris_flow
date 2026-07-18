#!/usr/bin/env bash
set -euo pipefail

# Rebuild the release-relevant source tree from Git objects, not from the
# developer worktree. The expensive browser, visual, audit and real-stack
# suites deliberately remain in the separate strict release gate.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SOURCE_ROOT="$(git -C "${SCRIPT_DIR}/.." rev-parse --show-toplevel)"
SOURCE_ROOT="$(cd "${SOURCE_ROOT}" && pwd -P)"
SOURCE_COMMIT="$(git -C "${SOURCE_ROOT}" rev-parse --verify HEAD^{commit})"
case "${AURIS_RELEASE_CHECK:-0}" in
  0)
    readiness_scope="base"
    ;;
  1)
    readiness_scope="release"
    ;;
  *)
    echo "AURIS_RELEASE_CHECK must be exactly 0 or 1 for the clean-clone gate." >&2
    exit 2
    ;;
esac

source_status="$(git -C "${SOURCE_ROOT}" status --porcelain=v1 --untracked-files=all)"
if [ -n "${source_status}" ]; then
  echo "Clean-clone gate refused: source worktree is not clean." >&2
  echo "Commit or remove every staged, unstaged and untracked release input first:" >&2
  printf '%s\n' "${source_status}" >&2
  exit 2
fi

for command_name in git uv node npm python3 mktemp; do
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    echo "Clean-clone gate requires '${command_name}' on PATH." >&2
    exit 2
  fi
done

uv_version="$(uv --version)"
case "${uv_version}" in
  "uv 0.10.0"*) ;;
  *)
    echo "Clean-clone gate requires uv 0.10.0; found: ${uv_version}" >&2
    exit 2
    ;;
esac

node_version="$(node --version)"
required_node_major="${AURIS_CLEAN_CLONE_NODE_MAJOR:-22}"
case "${node_version}" in
  "v${required_node_major}."*) ;;
  *)
    echo "Clean-clone gate requires Node.js ${required_node_major}.x; found: ${node_version}" >&2
    exit 2
    ;;
esac
npm_version="$(npm --version)"

python_request="${AURIS_CLEAN_CLONE_PYTHON:-3.12}"
temp_parent="${AURIS_CLEAN_CLONE_TEMP_PARENT:-${TMPDIR:-/tmp}}"
if [ ! -d "${temp_parent}" ]; then
  echo "Clean-clone temp parent does not exist: ${temp_parent}" >&2
  exit 2
fi
temp_parent="$(cd "${temp_parent}" && pwd -P)"
temp_root="$(mktemp -d "${temp_parent%/}/auris-flow-clean-clone.XXXXXX")"
temp_root="$(cd "${temp_root}" && pwd -P)"
clone_root="${temp_root}/repository"

cleanup() {
  exit_status=$?
  trap - EXIT HUP INT TERM
  if [ "${AURIS_KEEP_CLEAN_CLONE:-0}" = "1" ]; then
    echo "Preserved clean-clone workspace: ${temp_root}" >&2
  else
    case "${temp_root}" in
      "${temp_parent%/}/auris-flow-clean-clone."*)
        rm -rf -- "${temp_root}"
        ;;
      *)
        echo "Refusing unsafe clean-clone cleanup target: ${temp_root}" >&2
        exit_status=2
        ;;
    esac
  fi
  exit "${exit_status}"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

if [ -n "${AURIS_CLEAN_CLONE_CACHE_DIR:-}" ]; then
  cache_root="${AURIS_CLEAN_CLONE_CACHE_DIR}"
  mkdir -p "${cache_root}"
  cache_root="$(cd "${cache_root}" && pwd -P)"
else
  cache_root="${temp_root}/download-cache"
  mkdir -p "${cache_root}"
fi
mkdir -p "${cache_root}/uv" "${cache_root}/npm"
export UV_CACHE_DIR="${cache_root}/uv"
export npm_config_cache="${cache_root}/npm"
export UV_LINK_MODE=copy

# Inherited interpreter/module paths can make a clone pass by importing code
# from the caller's checkout. Each project must use the environment uv creates
# inside this clone.
unset PYTHONPATH PYTHONHOME VIRTUAL_ENV UV_PROJECT_ENVIRONMENT NODE_PATH

echo "Cloning committed source ${SOURCE_COMMIT} without local hard links..."
git clone --quiet --no-local --no-checkout -- "${SOURCE_ROOT}" "${clone_root}"
git -C "${clone_root}" checkout --quiet --detach "${SOURCE_COMMIT}"

clone_commit="$(git -C "${clone_root}" rev-parse --verify HEAD^{commit})"
if [ "${clone_commit}" != "${SOURCE_COMMIT}" ]; then
  echo "Clean clone HEAD mismatch: expected ${SOURCE_COMMIT}, got ${clone_commit}." >&2
  exit 1
fi
if [ "$(git -C "${clone_root}" rev-parse --is-shallow-repository)" != "false" ]; then
  echo "Clean clone unexpectedly has shallow history." >&2
  exit 1
fi
if [ -s "${clone_root}/.git/objects/info/alternates" ]; then
  echo "Clean clone unexpectedly borrows Git objects from the source repository." >&2
  exit 1
fi

initial_status="$(git -C "${clone_root}" status --porcelain=v1 --untracked-files=all)"
if [ -n "${initial_status}" ]; then
  echo "Fresh clone is not clean:" >&2
  printf '%s\n' "${initial_status}" >&2
  exit 1
fi

required_files=(
  "backend/pyproject.toml"
  "backend/uv.lock"
  "backend/scripts/verify_migrations.py"
  "backend/scripts/smoke_backend.py"
  "production/dagster/pyproject.toml"
  "production/dagster/uv.lock"
  "prototype/auris-flow-ui/package.json"
  "prototype/auris-flow-ui/package-lock.json"
  "doc/backend-spec/validate_backend_spec.py"
  "scripts/check_platform_readiness.py"
  "scripts/scan_secrets.py"
  "scripts/validate_public_audio_datasets.py"
  "scripts/verify_production_compose.py"
  "scripts/verify_clean_clone.sh"
)
for relative_path in "${required_files[@]}"; do
  if ! git -C "${clone_root}" ls-files --error-unmatch -- "${relative_path}" >/dev/null 2>&1; then
    echo "Required clean-clone input is not tracked: ${relative_path}" >&2
    exit 1
  fi
  if [ ! -s "${clone_root}/${relative_path}" ]; then
    echo "Required clean-clone input is missing or empty: ${relative_path}" >&2
    exit 1
  fi
done

required_prefixes=(
  "backend/app/"
  "backend/migrations/"
  "backend/tests/unit/"
  "backend/tests/contract/"
  "backend/tests/integration/"
  "production/dagster/src/"
  "production/dagster/tests/"
  "production/tests/"
  "prototype/auris-flow-ui/src/"
)
for relative_prefix in "${required_prefixes[@]}"; do
  if [ -z "$(git -C "${clone_root}" ls-files -- "${relative_prefix}")" ]; then
    echo "Required clean-clone source prefix has no tracked files: ${relative_prefix}" >&2
    exit 1
  fi
done

cd "${clone_root}"

echo "Installing locked backend environment (${uv_version}, Python ${python_request})..."
uv lock --check --project backend
uv sync --frozen --all-extras --project backend --python "${python_request}"
backend_python="${clone_root}/backend/.venv/bin/python"
if [ ! -x "${backend_python}" ]; then
  echo "Locked backend install did not create backend/.venv/bin/python." >&2
  exit 1
fi
"${backend_python}" -c \
  "import sys; assert sys.version_info[:2] == (3, 12), sys.version"

echo "Installing locked Dagster environment..."
uv lock --check --project production/dagster
uv sync --frozen --all-extras --project production/dagster --python "${python_request}"
dagster_python="${clone_root}/production/dagster/.venv/bin/python"
if [ ! -x "${dagster_python}" ]; then
  echo "Locked Dagster install did not create production/dagster/.venv/bin/python." >&2
  exit 1
fi
"${dagster_python}" -c \
  "import sys; assert sys.version_info[:2] == (3, 12), sys.version"

echo "Installing locked frontend environment (Node ${node_version}, npm ${npm_version})..."
npm ci --ignore-scripts --prefix prototype/auris-flow-ui

echo "Running release-tree quick gates from the clone..."
"${backend_python}" doc/backend-spec/validate_backend_spec.py
"${backend_python}" scripts/validate_public_audio_datasets.py
"${backend_python}" scripts/verify_production_compose.py
"${backend_python}" scripts/scan_secrets.py --history
if [ "${readiness_scope}" = "release" ]; then
  "${backend_python}" scripts/check_platform_readiness.py --release
else
  "${backend_python}" scripts/check_platform_readiness.py
  echo "Clean-clone functional gate checked base readiness; strict release authority remains a separate fail-closed gate."
fi

echo "Verifying migrations, backend compilation, tests and application smoke..."
"${backend_python}" backend/scripts/verify_migrations.py
"${backend_python}" -m compileall -q backend/app
"${backend_python}" -m pytest \
  backend/tests/unit backend/tests/contract backend/tests/integration
"${backend_python}" backend/scripts/smoke_backend.py
"${backend_python}" -m pytest production/tests

echo "Verifying the locked Dagster package and tests..."
uv run --frozen --all-extras --project production/dagster \
  pytest production/dagster/tests

echo "Building the frontend production bundle from npm lock data..."
npm run build --prefix prototype/auris-flow-ui
npm run bundle:check --prefix prototype/auris-flow-ui

final_commit="$(git rev-parse --verify HEAD^{commit})"
if [ "${final_commit}" != "${SOURCE_COMMIT}" ]; then
  echo "Verification changed clone HEAD: expected ${SOURCE_COMMIT}, got ${final_commit}." >&2
  exit 1
fi
final_status="$(git status --porcelain=v1 --untracked-files=all)"
if [ -n "${final_status}" ]; then
  echo "Clean clone became dirty after install/build/test:" >&2
  printf '%s\n' "${final_status}" >&2
  exit 1
fi
git diff --quiet --exit-code
git diff --cached --quiet --exit-code

evidence_output="${AURIS_CLEAN_CLONE_EVIDENCE:-${SOURCE_ROOT}/build/release-evidence/clean-clone.json}"
python3 - \
  "${evidence_output}" \
  "${SOURCE_COMMIT}" \
  "${uv_version}" \
  "${python_request}" \
  "${node_version}" \
  "${npm_version}" \
  "${readiness_scope}" <<'PY'
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path


output = Path(os.path.abspath(sys.argv[1]))
source_commit, uv_version, python_version, node_version, npm_version, readiness_scope = (
    sys.argv[2:]
)
if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", source_commit) is None:
    raise SystemExit("clean-clone evidence requires an exact source commit")
if readiness_scope not in {"base", "release"}:
    raise SystemExit("clean-clone evidence requires an exact readiness scope")
if output.exists() and (not output.is_file() or output.is_symlink()):
    raise SystemExit("clean-clone evidence output must be a regular file path")
output.parent.mkdir(parents=True, exist_ok=True)
payload = {
    "completed_at": datetime.now(UTC).isoformat(),
    "git_object_isolation": "clone-no-local-without-alternates",
    "readiness_scope": readiness_scope,
    "reproducibility_scope": "functional-locked-source",
    "schema_version": "auris.clean-clone-evidence.v1",
    "source_commit": source_commit,
    "status": "ok",
    "toolchain": {
        "node": node_version,
        "npm": npm_version,
        "python_request": python_version,
        "uv": uv_version,
    },
    "verified_steps": [
        "locked-dependency-install",
        "database-migrations",
        "backend-tests-and-smoke",
        "dagster-tests",
        "frontend-build-and-bundle-policy",
        f"{readiness_scope}-readiness",
        "secret-history-scan",
        "final-clean-tree",
    ],
}
temporary_name: str | None = None
try:
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=output.parent,
        prefix=f".{output.name}.",
        suffix=".tmp",
        delete=False,
    ) as temporary:
        temporary_name = temporary.name
        json.dump(payload, temporary, ensure_ascii=False, indent=2, sort_keys=True)
        temporary.write("\n")
        temporary.flush()
        os.fsync(temporary.fileno())
    os.replace(temporary_name, output)
    temporary_name = None
finally:
    if temporary_name is not None:
        Path(temporary_name).unlink(missing_ok=True)
PY

echo "clean clone verification ok: ${SOURCE_COMMIT}"
