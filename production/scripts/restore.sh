#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRODUCTION_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPOSITORY_ROOT="$(cd "${PRODUCTION_ROOT}/.." && pwd)"
COMPOSE_FILE="${PRODUCTION_ROOT}/compose.yaml"
BACKUP_TOOLS="${PRODUCTION_ROOT}/backup"
PYTHON="${PYTHON:-python3}"
ENV_FILE="${AURIS_COMPOSE_ENV_FILE:-${PRODUCTION_ROOT}/.env}"
BACKUP_ROOT=""
CONFIRMATION=""
QDRANT_MODE="snapshot"
REPORT_ROOT="${AURIS_RESTORE_REPORT_ROOT:-/var/tmp/auris-flow-restore-reports}"
REPORT_FILE=""
RESTORE_STEP="preflight"

usage() {
  cat <<'USAGE'
Usage: production/scripts/restore.sh --backup ABSOLUTE_DIR \
       --confirm BACKUP_ID [options]

Options:
  --env-file FILE                 Compose environment file
  --qdrant-mode snapshot          Restore compatible derived snapshots (default)
  --qdrant-mode rebuild-required  Leave Qdrant empty and require domain rebuild jobs
  --report-root ABSOLUTE_DIR      Diagnostic report directory
  -h, --help                      Show this help

The script refuses non-empty MySQL, MinIO, or Qdrant targets and refuses to run
while application writers are active. Redis is deliberately not restored.
USAGE
}

write_report() {
  local status="$1" message="$2"
  if [[ -n "${REPORT_FILE}" ]]; then
    printf '%s\t%s\t%s\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      "${status}" "${RESTORE_STEP}" "${message}" >>"${REPORT_FILE}"
  fi
}

fail() {
  write_report failed "$1"
  printf 'restore failed at %s: %s\n' "${RESTORE_STEP}" "$1" >&2
  if [[ -n "${REPORT_FILE}" ]]; then
    printf 'diagnostic report: %s\n' "${REPORT_FILE}" >&2
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
    --confirm)
      (($# >= 2)) || fail "--confirm requires a value"
      CONFIRMATION="$2"
      shift 2
      ;;
    --env-file)
      (($# >= 2)) || fail "--env-file requires a value"
      ENV_FILE="$2"
      shift 2
      ;;
    --qdrant-mode)
      (($# >= 2)) || fail "--qdrant-mode requires a value"
      QDRANT_MODE="$2"
      shift 2
      ;;
    --report-root)
      (($# >= 2)) || fail "--report-root requires a value"
      REPORT_ROOT="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *) fail "unknown option: $1" ;;
  esac
done

for command_name in docker git gzip cmp mktemp "${PYTHON}"; do
  command -v "${command_name}" >/dev/null 2>&1 || fail "required command not found: ${command_name}"
done
has_control_character "${BACKUP_ROOT}" && fail "backup path contains a control character"
has_control_character "${REPORT_ROOT}" && fail "report path contains a control character"
has_control_character "${ENV_FILE}" && fail "Compose env path contains a control character"
[[ "${BACKUP_ROOT}" == /* ]] || fail "--backup must be an absolute path"
[[ "${REPORT_ROOT}" == /* ]] || fail "--report-root must be an absolute path"
[[ "${QDRANT_MODE}" == "snapshot" || "${QDRANT_MODE}" == "rebuild-required" ]] || fail \
  "--qdrant-mode must be snapshot or rebuild-required"
[[ -d "${BACKUP_ROOT}" && ! -L "${BACKUP_ROOT}" ]] || fail \
  "backup root must be a real directory, not a symlink"
BACKUP_ROOT="$(cd "${BACKUP_ROOT}" && pwd -P)"
case "${BACKUP_ROOT}" in
  /|"${REPOSITORY_ROOT}"|"${PRODUCTION_ROOT}") fail "backup path is too broad or unsafe" ;;
esac
[[ -f "${ENV_FILE}" && ! -L "${ENV_FILE}" ]] || fail "Compose env file is missing or unsafe"
mkdir -p "${REPORT_ROOT}"
[[ -d "${REPORT_ROOT}" && ! -L "${REPORT_ROOT}" ]] || fail "report root must be a real directory"
REPORT_ROOT="$(cd "${REPORT_ROOT}" && pwd -P)"
case "${REPORT_ROOT}" in
  /|"${REPOSITORY_ROOT}"|"${PRODUCTION_ROOT}") fail "report root is too broad or unsafe" ;;
esac

RESTORE_STEP="verify-backup"
"${PYTHON}" "${BACKUP_TOOLS}/manifest.py" verify --root "${BACKUP_ROOT}" >/dev/null || fail \
  "manifest or artifact verification failed"
gzip -t "${BACKUP_ROOT}/mysql/all-databases.sql.gz" || fail "MySQL dump is corrupt"
inspect_json="$("${PYTHON}" "${BACKUP_TOOLS}/manifest.py" inspect --root "${BACKUP_ROOT}")"
backup_id="$(printf '%s' "${inspect_json}" | "${PYTHON}" -c \
  'import json,sys; print(json.load(sys.stdin)["backup_id"])')"
backup_commit="$(printf '%s' "${inspect_json}" | "${PYTHON}" -c \
  'import json,sys; print(json.load(sys.stdin)["git_commit"])')"
[[ "${CONFIRMATION}" == "${backup_id}" ]] || fail \
  "confirmation must exactly equal backup id ${backup_id}"
current_commit="$(git -C "${REPOSITORY_ROOT}" rev-parse HEAD)"
[[ "${current_commit}" == "${backup_commit}" ]] || fail \
  "recovery checkout does not match backup commit ${backup_commit}"

REPORT_FILE="${REPORT_ROOT}/${backup_id}-$(date -u +%Y%m%dT%H%M%SZ).tsv"
printf 'timestamp_utc\tstatus\tstep\tmessage\n' >"${REPORT_FILE}"
write_report started "checksum verified and operator confirmation accepted"

compose() {
  docker compose \
    --project-directory "${PRODUCTION_ROOT}" \
    --env-file "${ENV_FILE}" \
    -f "${COMPOSE_FILE}" "$@"
}

RESTORE_STEP="compose-preflight"
compose config --quiet || fail "Compose configuration is invalid"
running_services="$(compose ps --status running --services)"
for required_service in mysql minio qdrant redis; do
  if ! printf '%s\n' "${running_services}" | grep -Fxq "${required_service}"; then
    fail "required dependency service is not running: ${required_service}"
  fi
done
for writer_service in edge bff worker keycloak dagster-code dagster-webserver dagster-daemon migrate; do
  if printf '%s\n' "${running_services}" | grep -Fxq "${writer_service}"; then
    fail "writer service ${writer_service} is running; restore requires an offline application tier"
  fi
done
write_report passed "dependency services are running and application writers are stopped"

RESTORE_STEP="empty-target-preflight"
target_counts="$(mktemp "${REPORT_ROOT}/target-counts.XXXXXX")"
compose exec -T mysql sh -c '
  export MYSQL_PWD="$(cat /run/secrets/mysql_root_password)"
  exec mysql --protocol=tcp --host=127.0.0.1 --user=root \
    --batch --raw --skip-column-names
' <"${BACKUP_TOOLS}/mysql_counts.sql" >"${target_counts}"
if awk -F '\t' '$3 != "0" { found=1 } END { exit found ? 0 : 1 }' "${target_counts}"; then
  fail "target MySQL contains rows; no overwrite escape hatch is provided"
fi
target_minio_listing="$(mktemp "${REPORT_ROOT}/target-minio.XXXXXX")"
compose exec -T minio mc ls --recursive --versions --json local/auris-flow \
  >"${target_minio_listing}"
if [[ -s "${target_minio_listing}" ]]; then
  fail "target MinIO bucket contains object versions; refusing overwrite"
fi
compose run --rm --no-deps \
  --user "$(id -u):$(id -g)" \
  -v "${BACKUP_TOOLS}/qdrant_snapshots.py:/opt/auris/qdrant-snapshots.py:ro" \
  --entrypoint python bff /opt/auris/qdrant-snapshots.py assert-empty >/dev/null || fail \
  "target Qdrant contains collections"
write_report passed "MySQL rows, MinIO versions, and Qdrant collections are empty"

RESTORE_STEP="mysql-authority"
gzip -dc "${BACKUP_ROOT}/mysql/all-databases.sql.gz" | compose exec -T mysql sh -c '
  export MYSQL_PWD="$(cat /run/secrets/mysql_root_password)"
  exec mysql --protocol=tcp --host=127.0.0.1 --user=root \
    --default-character-set=utf8mb4
' || fail "MySQL authoritative restore failed"
write_report passed "business, Keycloak, and Dagster schemas restored"

RESTORE_STEP="minio-authority"
minio_restore_script="$(mktemp "${REPORT_ROOT}/minio-restore.XXXXXX")"
"${PYTHON}" "${BACKUP_TOOLS}/minio_versions.py" emit-restore-shell \
  --plan "${BACKUP_ROOT}/minio/versions.json" \
  --output "${minio_restore_script}" || fail "could not build the trusted MinIO replay plan"
compose run --rm --no-deps \
  --user "$(id -u):$(id -g)" \
  -v "${BACKUP_ROOT}:/backup:ro" \
  -v "${minio_restore_script}:/opt/auris/minio-restore.sh:ro" \
  --entrypoint /bin/sh minio-bootstrap /opt/auris/minio-restore.sh || fail \
  "MinIO version replay failed"
write_report passed "all content generations and delete markers replayed"

RESTORE_STEP="qdrant-derived-index"
if [[ "${QDRANT_MODE}" == "snapshot" ]]; then
  compose run --rm --no-deps \
    --user "$(id -u):$(id -g)" \
    -v "${BACKUP_ROOT}:/backup:ro" \
    -v "${BACKUP_TOOLS}/qdrant_snapshots.py:/opt/auris/qdrant-snapshots.py:ro" \
    --entrypoint python bff \
    /opt/auris/qdrant-snapshots.py restore --input /backup/qdrant || fail \
    "Qdrant derived snapshot restore failed; MySQL and MinIO remain authoritative"
  write_report passed "derived Qdrant snapshots restored after authoritative sources"
else
  write_report action-required \
    "Qdrant left empty; start the app and submit governed knowledge-index build-runs/outbox reconciliation"
fi

RESTORE_STEP="consistency-verification"
restored_counts="$(mktemp "${REPORT_ROOT}/restored-counts.XXXXXX")"
compose exec -T mysql sh -c '
  export MYSQL_PWD="$(cat /run/secrets/mysql_root_password)"
  exec mysql --protocol=tcp --host=127.0.0.1 --user=root \
    --batch --raw --skip-column-names
' <"${BACKUP_TOOLS}/mysql_counts.sql" >"${restored_counts}"
cmp -s "${BACKUP_ROOT}/mysql/table-counts.tsv" "${restored_counts}" || fail \
  "restored MySQL table counts differ from the quiesced backup"
restored_minio_listing="$(mktemp "${REPORT_ROOT}/restored-minio.XXXXXX")"
compose exec -T minio mc ls --recursive --versions --json local/auris-flow \
  >"${restored_minio_listing}"
"${PYTHON}" "${BACKUP_TOOLS}/minio_versions.py" compare-listing \
  --plan "${BACKUP_ROOT}/minio/versions.json" \
  --listing "${restored_minio_listing}" >/dev/null || fail \
  "restored MinIO semantic version history differs from backup"
if [[ "${QDRANT_MODE}" == "snapshot" ]]; then
  compose run --rm --no-deps \
    --user "$(id -u):$(id -g)" \
    -v "${BACKUP_ROOT}:/backup:ro" \
    -v "${BACKUP_TOOLS}/qdrant_snapshots.py:/opt/auris/qdrant-snapshots.py:ro" \
    --entrypoint python bff \
    /opt/auris/qdrant-snapshots.py verify-counts --input /backup/qdrant >/dev/null || fail \
    "restored Qdrant collection counts differ from backup"
fi
write_report passed "authority counts and derived-index counts are consistent"

RESTORE_STEP="complete"
write_report complete \
  "Redis cache intentionally omitted; run migrations for the selected release, then start application services"
rm -f \
  "${target_counts}" \
  "${target_minio_listing}" \
  "${minio_restore_script}" \
  "${restored_counts}" \
  "${restored_minio_listing}"
printf 'Restore complete for %s. Report: %s\n' "${backup_id}" "${REPORT_FILE}"
if [[ "${QDRANT_MODE}" == "rebuild-required" ]]; then
  printf 'Qdrant remains empty by design; execute governed rebuild jobs before declaring readiness.\n'
fi
printf 'Redis was not restored because it is a disposable cache, never a business authority.\n'
