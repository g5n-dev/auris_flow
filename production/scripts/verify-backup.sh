#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRODUCTION_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPOSITORY_ROOT="$(cd "${PRODUCTION_ROOT}/.." && pwd)"
COMPOSE_FILE="${PRODUCTION_ROOT}/compose.yaml"
DOCKER_CONTEXT_NAME="default"
BACKUP_TOOLS="${PRODUCTION_ROOT}/backup"
RELEASE_BUNDLE_TOOL="${REPOSITORY_ROOT}/scripts/release_bundle.py"
DEADLINE_RUNNER="${REPOSITORY_ROOT}/scripts/run_with_deadline.py"
PYTHON="${PYTHON:-python3}"
ENV_FILE="${AURIS_COMPOSE_ENV_FILE:-${PRODUCTION_ROOT}/.env}"
BACKUP_ROOT=""
RUN_DRILL=false
CLEANUP_ON_SUCCESS=false
DRILL_PROJECT=""
ALLOW_RELEASE_MIGRATION_FROM=""
VALIDATION_SCRIPT=""
DRILL_PULL_TIMEOUT="${AURIS_RESTORE_DRILL_PULL_TIMEOUT:-900}"
DRILL_WAIT_TIMEOUT="${AURIS_RESTORE_DRILL_WAIT_TIMEOUT:-240}"
DRILL_RUN_TIMEOUT="${AURIS_RESTORE_DRILL_RUN_TIMEOUT:-120}"
DRILL_CLEANUP_TIMEOUT="${AURIS_RESTORE_DRILL_CLEANUP_TIMEOUT:-60}"

usage() {
  cat <<'USAGE'
Usage: production/scripts/verify-backup.sh --backup ABSOLUTE_DIR [options]

Options:
  --drill                 Restore into a newly named Compose project and verify counts
  --cleanup-on-success    Destroy only the generated drill project/volumes after success
  --env-file FILE         Compose environment file required by --drill
  --allow-release-migration-from COMMIT
                          Drill an exact predecessor only when the installed signed
                          compatibility policy lists its tag, commit and metadata hash
  -h, --help              Show this help

Without --drill this command is offline: it verifies the canonical manifest,
every SHA-256, the MySQL gzip/structure, and MinIO/Qdrant recovery metadata.
USAGE
}

fail() {
  printf 'backup verification failed: %s\n' "$1" >&2
  if [[ -n "${DRILL_PROJECT}" ]]; then
    printf 'isolated drill project retained for diagnosis: %s\n' "${DRILL_PROJECT}" >&2
  fi
  exit 2
}

cleanup() {
  local status="$1" cleanup_failed=0
  trap - EXIT INT TERM
  set +e
  if [[ -n "${VALIDATION_SCRIPT}" && -e "${VALIDATION_SCRIPT}" ]]; then
    if ! rm -f -- "${VALIDATION_SCRIPT}"; then
      printf 'backup verification cleanup failed for validation script: %s\n' \
        "${VALIDATION_SCRIPT}" >&2
      cleanup_failed=1
    fi
  fi
  if [[ "${status}" -eq 0 && "${CLEANUP_ON_SUCCESS}" == true && \
    -n "${DRILL_PROJECT}" ]]; then
    if compose_drill_with_deadline "${DRILL_CLEANUP_TIMEOUT}" \
      "clean restore drill project" \
      down --volumes --remove-orphans --timeout 20 >/dev/null 2>&1; then
      printf 'Removed isolated project and volumes: %s\n' "${DRILL_PROJECT}"
      DRILL_PROJECT=""
    else
      printf 'drill passed but exact-project cleanup failed: %s\n' \
        "${DRILL_PROJECT}" >&2
      cleanup_failed=1
    fi
  fi
  if [ "${status}" -eq 0 ] && [ "${cleanup_failed}" -ne 0 ]; then
    status=1
  fi
  exit "${status}"
}
trap 'cleanup "$?"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

has_control_character() {
  [[ "$1" == *$'\n'* || "$1" == *$'\r'* || "$1" == *$'\t'* ]]
}

paths_overlap() {
  local first="${1%/}/" second="${2%/}/"
  [[ "${first}" == "${second}"* || "${second}" == "${first}"* ]]
}

while (($#)); do
  case "$1" in
    --backup)
      (($# >= 2)) || fail "--backup requires a value"
      BACKUP_ROOT="$2"
      shift 2
      ;;
    --drill)
      RUN_DRILL=true
      shift
      ;;
    --cleanup-on-success)
      CLEANUP_ON_SUCCESS=true
      shift
      ;;
    --env-file)
      (($# >= 2)) || fail "--env-file requires a value"
      ENV_FILE="$2"
      shift 2
      ;;
    --allow-release-migration-from)
      (($# >= 2)) || fail "--allow-release-migration-from requires a value"
      ALLOW_RELEASE_MIGRATION_FROM="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *) fail "unknown option: $1" ;;
  esac
done

command -v "${PYTHON}" >/dev/null 2>&1 || fail "Python is required"
has_control_character "${BACKUP_ROOT}" && fail "backup path contains a control character"
has_control_character "${ENV_FILE}" && fail "Compose env path contains a control character"
if [[ -n "${ALLOW_RELEASE_MIGRATION_FROM}" && \
  ! "${ALLOW_RELEASE_MIGRATION_FROM}" =~ ^[0-9a-f]{40}$ && \
  ! "${ALLOW_RELEASE_MIGRATION_FROM}" =~ ^[0-9a-f]{64}$ ]]; then
  fail "--allow-release-migration-from must be a complete lowercase Git id"
fi
[[ "${BACKUP_ROOT}" == /* ]] || fail "--backup must be an absolute path"
[[ -d "${BACKUP_ROOT}" && ! -L "${BACKUP_ROOT}" ]] || fail \
  "backup root must be a real directory, not a symlink"
BACKUP_ROOT="$(cd "${BACKUP_ROOT}" && pwd -P)"
[[ "${BACKUP_ROOT}" != "/" ]] || fail "backup root is too broad"
paths_overlap "${BACKUP_ROOT}" "${REPOSITORY_ROOT}" && fail \
  "backup path must not be an ancestor or descendant of the release bundle"

"${PYTHON}" "${BACKUP_TOOLS}/manifest.py" verify --root "${BACKUP_ROOT}"
"${PYTHON}" "${BACKUP_TOOLS}/mysql_dump.py" verify \
  --input "${BACKUP_ROOT}/mysql/all-databases.sql.gz"
VALIDATION_SCRIPT="$(mktemp "${TMPDIR:-/tmp}/auris-minio-validate.XXXXXX")"
"${PYTHON}" "${BACKUP_TOOLS}/minio_versions.py" emit-restore-shell \
  --plan "${BACKUP_ROOT}/minio/versions.json" \
  --output "${VALIDATION_SCRIPT}"
bash -n "${VALIDATION_SCRIPT}"
rm -f -- "${VALIDATION_SCRIPT}"
VALIDATION_SCRIPT=""
"${PYTHON}" "${BACKUP_TOOLS}/qdrant_snapshots.py" validate \
  --input "${BACKUP_ROOT}/qdrant"
printf 'Offline backup verification passed.\n'

if [[ "${RUN_DRILL}" != true ]]; then
  exit 0
fi

command -v docker >/dev/null 2>&1 || fail "Docker is required for --drill"
command -v cosign >/dev/null 2>&1 || fail "Cosign is required for --drill"
for timeout_value in \
  "${DRILL_PULL_TIMEOUT}" \
  "${DRILL_WAIT_TIMEOUT}" \
  "${DRILL_RUN_TIMEOUT}" \
  "${DRILL_CLEANUP_TIMEOUT}"; do
  [[ "${timeout_value}" =~ ^[1-9][0-9]*$ ]] || fail \
    "restore drill timeouts must be positive integers"
done
[[ -f "${DEADLINE_RUNNER}" && ! -L "${DEADLINE_RUNNER}" ]] || fail \
  "deadline runner is missing or unsafe"
[[ -z "${DOCKER_HOST:-}" && -z "${DOCKER_CONTEXT:-}" && \
  -z "${COMPOSE_PROJECT_NAME:-}" ]] || fail \
  "DOCKER_HOST, DOCKER_CONTEXT and COMPOSE_PROJECT_NAME overrides are forbidden"
docker --context "${DOCKER_CONTEXT_NAME}" info >/dev/null 2>&1 || fail \
  "the bound Docker context is unavailable: ${DOCKER_CONTEXT_NAME}"
[[ -f "${RELEASE_BUNDLE_TOOL}" && ! -L "${RELEASE_BUNDLE_TOOL}" ]] || fail \
  "release bundle verifier is missing or unsafe"
"${PYTHON}" "${RELEASE_BUNDLE_TOOL}" verify \
  --bundle-root "${REPOSITORY_ROOT}" \
  --verify-signature >/dev/null || fail \
  "signed release metadata verification failed"
[[ -f "${ENV_FILE}" && ! -L "${ENV_FILE}" ]] || fail \
  "Compose env file is missing or unsafe: ${ENV_FILE}"
inspect_json="$("${PYTHON}" "${BACKUP_TOOLS}/manifest.py" inspect --root "${BACKUP_ROOT}")"
backup_id="$(printf '%s' "${inspect_json}" | "${PYTHON}" -c \
  'import json,sys; print(json.load(sys.stdin)["backup_id"])')"
drill_suffix="$("${PYTHON}" -c 'import secrets; print(secrets.token_hex(6))')"
DRILL_PROJECT="auris-flow-restore-drill-${drill_suffix}"
[[ "${DRILL_PROJECT}" =~ ^auris-flow-restore-drill-[0-9a-f]{12}$ ]] || fail \
  "unsafe drill project name"

compose_drill_with_deadline() {
  local timeout_seconds="$1" label="$2"
  shift 2
  COMPOSE_PROJECT_NAME="${DRILL_PROJECT}" \
    "${PYTHON}" "${DEADLINE_RUNNER}" \
    --timeout-seconds "${timeout_seconds}" \
    --label "${label}" -- \
    docker --context "${DOCKER_CONTEXT_NAME}" compose \
    --project-name "${DRILL_PROJECT}" \
    --project-directory "${PRODUCTION_ROOT}" \
    --env-file "${ENV_FILE}" \
    -f "${COMPOSE_FILE}" "$@"
}

compose_drill_with_deadline "${DRILL_RUN_TIMEOUT}" \
  "validate restore drill Compose" config --quiet || fail \
  "Compose configuration is invalid"
printf 'Pulling digest-pinned recovery images for isolated drill %s...\n' "${DRILL_PROJECT}"
compose_drill_with_deadline "${DRILL_PULL_TIMEOUT}" \
  "pull restore drill images" \
  pull mysql db-bootstrap redis minio minio-bootstrap qdrant bff || fail \
  "could not pull the signed recovery-side images"
compose_drill_with_deadline "${DRILL_WAIT_TIMEOUT}" \
  "start restore drill authority services" \
  up --detach --no-deps --wait --wait-timeout "${DRILL_WAIT_TIMEOUT}" \
  mysql redis minio qdrant || fail \
  "could not start an empty dependency stack"
compose_drill_with_deadline "${DRILL_RUN_TIMEOUT}" \
  "run restore drill database bootstrap" \
  run --rm --no-deps db-bootstrap || fail \
  "restore drill database bootstrap failed"
compose_drill_with_deadline "${DRILL_RUN_TIMEOUT}" \
  "run restore drill object-storage bootstrap" \
  run --rm --no-deps minio-bootstrap || fail \
  "restore drill object-storage bootstrap failed"
"${PYTHON}" "${RELEASE_BUNDLE_TOOL}" verify-running-images \
  --bundle-root "${REPOSITORY_ROOT}" \
  --project-directory "${PRODUCTION_ROOT}" \
  --env-file "${ENV_FILE}" \
  --project-name "${DRILL_PROJECT}" \
  --docker-context "${DOCKER_CONTEXT_NAME}" \
  --service mysql \
  --service minio \
  --service qdrant \
  --service redis \
  --all-running-release-services \
  --verify-signature \
  >/dev/null || fail "drill authority-service images do not match release digests"

restore_arguments=(
  --backup "${BACKUP_ROOT}"
  --confirm "${backup_id}"
  --env-file "${ENV_FILE}"
  --qdrant-mode snapshot
  --project-name "${DRILL_PROJECT}"
  --docker-context "${DOCKER_CONTEXT_NAME}"
)
if [[ -n "${ALLOW_RELEASE_MIGRATION_FROM}" ]]; then
  restore_arguments+=(--allow-release-migration-from "${ALLOW_RELEASE_MIGRATION_FROM}")
fi
AURIS_RESTORE_REPORT_ROOT="${TMPDIR:-/tmp}/auris-flow-restore-reports-${DRILL_PROJECT}" \
  "${SCRIPT_DIR}/restore.sh" "${restore_arguments[@]}" || fail \
  "isolated restore drill failed"

printf 'Isolated restore drill passed for project %s.\n' "${DRILL_PROJECT}"
if [[ "${CLEANUP_ON_SUCCESS}" == true ]]; then
  printf 'Exact-project cleanup requested; removing it through the bounded exit handler.\n'
else
  printf 'Drill project retained for inspection; remove it explicitly with:\n'
  printf '  docker --context %q compose --project-name %q --project-directory %q --env-file %q -f %q down --volumes --remove-orphans\n' \
    "${DOCKER_CONTEXT_NAME}" "${DRILL_PROJECT}" "${PRODUCTION_ROOT}" "${ENV_FILE}" "${COMPOSE_FILE}"
fi
