#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRODUCTION_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
COMPOSE_FILE="${PRODUCTION_ROOT}/compose.yaml"
BACKUP_TOOLS="${PRODUCTION_ROOT}/backup"
PYTHON="${PYTHON:-python3}"
ENV_FILE="${AURIS_COMPOSE_ENV_FILE:-${PRODUCTION_ROOT}/.env}"
BACKUP_ROOT=""
RUN_DRILL=false
CLEANUP_ON_SUCCESS=false
DRILL_PROJECT=""

usage() {
  cat <<'USAGE'
Usage: production/scripts/verify-backup.sh --backup ABSOLUTE_DIR [options]

Options:
  --drill                 Restore into a newly named Compose project and verify counts
  --cleanup-on-success    Destroy only the generated drill project/volumes after success
  --env-file FILE         Compose environment file required by --drill
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

has_control_character() {
  [[ "$1" == *$'\n'* || "$1" == *$'\r'* || "$1" == *$'\t'* ]]
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
[[ "${BACKUP_ROOT}" == /* ]] || fail "--backup must be an absolute path"
[[ -d "${BACKUP_ROOT}" && ! -L "${BACKUP_ROOT}" ]] || fail \
  "backup root must be a real directory, not a symlink"
BACKUP_ROOT="$(cd "${BACKUP_ROOT}" && pwd -P)"
[[ "${BACKUP_ROOT}" != "/" ]] || fail "backup root is too broad"

"${PYTHON}" "${BACKUP_TOOLS}/manifest.py" verify --root "${BACKUP_ROOT}"
"${PYTHON}" "${BACKUP_TOOLS}/mysql_dump.py" verify \
  --input "${BACKUP_ROOT}/mysql/all-databases.sql.gz"
validation_script="$(mktemp "${TMPDIR:-/tmp}/auris-minio-validate.XXXXXX")"
"${PYTHON}" "${BACKUP_TOOLS}/minio_versions.py" emit-restore-shell \
  --plan "${BACKUP_ROOT}/minio/versions.json" \
  --output "${validation_script}"
bash -n "${validation_script}"
rm -f "${validation_script}"
"${PYTHON}" "${BACKUP_TOOLS}/qdrant_snapshots.py" validate \
  --input "${BACKUP_ROOT}/qdrant"
printf 'Offline backup verification passed.\n'

if [[ "${RUN_DRILL}" != true ]]; then
  exit 0
fi

command -v docker >/dev/null 2>&1 || fail "Docker is required for --drill"
[[ -f "${ENV_FILE}" && ! -L "${ENV_FILE}" ]] || fail \
  "Compose env file is missing or unsafe: ${ENV_FILE}"
inspect_json="$("${PYTHON}" "${BACKUP_TOOLS}/manifest.py" inspect --root "${BACKUP_ROOT}")"
backup_id="$(printf '%s' "${inspect_json}" | "${PYTHON}" -c \
  'import json,sys; print(json.load(sys.stdin)["backup_id"])')"
drill_suffix="$("${PYTHON}" -c 'import secrets; print(secrets.token_hex(6))')"
DRILL_PROJECT="auris-flow-restore-drill-${drill_suffix}"
[[ "${DRILL_PROJECT}" =~ ^auris-flow-restore-drill-[0-9a-f]{12}$ ]] || fail \
  "unsafe drill project name"

compose_drill() {
  COMPOSE_PROJECT_NAME="${DRILL_PROJECT}" docker compose \
    --project-name "${DRILL_PROJECT}" \
    --project-directory "${PRODUCTION_ROOT}" \
    --env-file "${ENV_FILE}" \
    -f "${COMPOSE_FILE}" "$@"
}

compose_drill config --quiet || fail "Compose configuration is invalid"
printf 'Building the recovery-side application image for isolated drill %s...\n' "${DRILL_PROJECT}"
compose_drill build bff || fail "could not build the recovery-side image"
compose_drill up -d --wait mysql db-bootstrap redis minio minio-bootstrap qdrant || fail \
  "could not start an empty dependency stack"

COMPOSE_PROJECT_NAME="${DRILL_PROJECT}" \
  AURIS_RESTORE_REPORT_ROOT="${TMPDIR:-/tmp}/auris-flow-restore-reports-${DRILL_PROJECT}" \
  "${SCRIPT_DIR}/restore.sh" \
    --backup "${BACKUP_ROOT}" \
    --confirm "${backup_id}" \
    --env-file "${ENV_FILE}" \
    --qdrant-mode snapshot || fail "isolated restore drill failed"

printf 'Isolated restore drill passed for project %s.\n' "${DRILL_PROJECT}"
if [[ "${CLEANUP_ON_SUCCESS}" == true ]]; then
  compose_drill down --volumes --remove-orphans || fail \
    "drill passed but exact-project cleanup failed"
  printf 'Removed isolated project and volumes: %s\n' "${DRILL_PROJECT}"
  DRILL_PROJECT=""
else
  printf 'Drill project retained for inspection; remove it explicitly with:\n'
  printf '  COMPOSE_PROJECT_NAME=%q docker compose --project-name %q --project-directory %q --env-file %q -f %q down --volumes --remove-orphans\n' \
    "${DRILL_PROJECT}" "${DRILL_PROJECT}" "${PRODUCTION_ROOT}" "${ENV_FILE}" "${COMPOSE_FILE}"
fi
