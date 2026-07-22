#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRODUCTION_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPOSITORY_ROOT="$(cd "${PRODUCTION_ROOT}/.." && pwd)"
COMPOSE_FILE="${PRODUCTION_ROOT}/compose.yaml"
PRODUCTION_PROJECT_NAME="auris-flow"
DOCKER_CONTEXT_NAME="default"
BACKUP_TOOLS="${PRODUCTION_ROOT}/backup"
RESTORE_STATE_TOOL="${BACKUP_TOOLS}/restore_state.py"
RELEASE_BUNDLE_TOOL="${REPOSITORY_ROOT}/scripts/release_bundle.py"
RELEASE_METADATA_FILE="${PRODUCTION_ROOT}/release-metadata.json"
RELEASE_METADATA_SIGNATURE="${PRODUCTION_ROOT}/release-metadata.sigstore.json"
PYTHON="${PYTHON:-python3}"
ENV_FILE="${AURIS_COMPOSE_ENV_FILE:-${PRODUCTION_ROOT}/.env}"
SECRETS_DIR="${AURIS_SECRETS_DIR:-${PRODUCTION_ROOT}/secrets}"
MANIFEST_VERIFY_KEY_FILE="${AURIS_BACKUP_MANIFEST_VERIFY_KEY_FILE:-${SECRETS_DIR}/backup_manifest_signing_public_key.pem}"
RESTORE_ATTESTATION_VERIFY_KEY_FILE="${AURIS_RESTORE_ATTESTATION_VERIFY_KEY_FILE:-${SECRETS_DIR}/restore_attestation_signing_public_key.pem}"
BACKUP_ROOT=""
SOURCE_BACKUP_ROOT=""
RESTORE_SNAPSHOT_ROOT=""
CONFIRMATION=""
QDRANT_MODE="snapshot"
ALLOW_RELEASE_MIGRATION_FROM=""
REPORT_ROOT="${AURIS_RESTORE_REPORT_ROOT:-/var/tmp/auris-flow-restore-reports}"
REPORT_FILE=""
RESTORE_STEP="preflight"
RESTORE_PENDING_EXIT_CODE=3

usage() {
  cat <<'USAGE'
Usage: production/scripts/restore.sh --backup ABSOLUTE_DIR \
       --confirm BACKUP_ID [options]

Options:
  --env-file FILE                 Compose environment file
  --manifest-public-key FILE      Deployment-owned Ed25519 trust-anchor public key
  --restore-attestation-public-key FILE
                                  Deployment-owned restore-attestation public key
  --project-name NAME             Exact Compose project (default: auris-flow)
  --docker-context NAME           Exact Docker context (default: default)
  --qdrant-mode snapshot          Restore compatible derived snapshots (default)
  --qdrant-mode rebuild-required  Leave Qdrant empty and require domain rebuild jobs
  --allow-release-migration-from COMMIT
                                  Explicitly restore an older backup into this newer
                                  signed release only when its exact tag, commit and
                                  metadata hash are in the signed compatibility policy
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

cleanup_restore_snapshot() {
  if [[ -n "${RESTORE_SNAPSHOT_ROOT}" && -d "${RESTORE_SNAPSHOT_ROOT}" && \
    ! -L "${RESTORE_SNAPSHOT_ROOT}" ]]; then
    "${PYTHON}" "${BACKUP_TOOLS}/manifest.py" destroy-snapshot \
      --snapshot-root "${RESTORE_SNAPSHOT_ROOT}" >/dev/null 2>&1 || \
      printf 'restore warning: private backup snapshot requires manual cleanup: %s\n' \
        "${RESTORE_SNAPSHOT_ROOT}" >&2
  fi
}

trap cleanup_restore_snapshot EXIT

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
    --manifest-public-key)
      (($# >= 2)) || fail "--manifest-public-key requires a value"
      MANIFEST_VERIFY_KEY_FILE="$2"
      shift 2
      ;;
    --restore-attestation-public-key)
      (($# >= 2)) || fail "--restore-attestation-public-key requires a value"
      RESTORE_ATTESTATION_VERIFY_KEY_FILE="$2"
      shift 2
      ;;
    --project-name)
      (($# >= 2)) || fail "--project-name requires a value"
      PRODUCTION_PROJECT_NAME="$2"
      shift 2
      ;;
    --docker-context)
      (($# >= 2)) || fail "--docker-context requires a value"
      DOCKER_CONTEXT_NAME="$2"
      shift 2
      ;;
    --qdrant-mode)
      (($# >= 2)) || fail "--qdrant-mode requires a value"
      QDRANT_MODE="$2"
      shift 2
      ;;
    --allow-release-migration-from)
      (($# >= 2)) || fail "--allow-release-migration-from requires a value"
      ALLOW_RELEASE_MIGRATION_FROM="$2"
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

for command_name in docker gzip cmp mktemp cosign openssl "${PYTHON}"; do
  command -v "${command_name}" >/dev/null 2>&1 || fail "required command not found: ${command_name}"
done
[[ -f "${COMPOSE_FILE}" && ! -L "${COMPOSE_FILE}" ]] || fail \
  "Compose file is missing or unsafe"
[[ -f "${RELEASE_BUNDLE_TOOL}" && ! -L "${RELEASE_BUNDLE_TOOL}" ]] || fail \
  "release bundle verifier is missing or unsafe"
[[ -f "${RELEASE_METADATA_FILE}" && ! -L "${RELEASE_METADATA_FILE}" ]] || fail \
  "signed release metadata is missing or unsafe"
[[ -f "${RELEASE_METADATA_SIGNATURE}" && ! -L "${RELEASE_METADATA_SIGNATURE}" ]] || fail \
  "release metadata Sigstore bundle is missing or unsafe"
[[ -f "${RESTORE_STATE_TOOL}" && ! -L "${RESTORE_STATE_TOOL}" ]] || fail \
  "restore state transition tool is missing or unsafe"
[[ "${PRODUCTION_PROJECT_NAME}" =~ ^[a-z0-9][a-z0-9_-]{0,62}$ ]] || fail \
  "Compose project name is invalid"
[[ "${DOCKER_CONTEXT_NAME}" =~ ^[A-Za-z0-9_.-]+$ ]] || fail \
  "Docker context name is invalid"
[[ -z "${DOCKER_HOST:-}" && -z "${DOCKER_CONTEXT:-}" && \
  -z "${COMPOSE_PROJECT_NAME:-}" ]] || fail \
  "DOCKER_HOST, DOCKER_CONTEXT and COMPOSE_PROJECT_NAME overrides are forbidden"
docker --context "${DOCKER_CONTEXT_NAME}" info >/dev/null 2>&1 || fail \
  "the bound Docker context is unavailable: ${DOCKER_CONTEXT_NAME}"
has_control_character "${BACKUP_ROOT}" && fail "backup path contains a control character"
has_control_character "${REPORT_ROOT}" && fail "report path contains a control character"
has_control_character "${ENV_FILE}" && fail "Compose env path contains a control character"
has_control_character "${MANIFEST_VERIFY_KEY_FILE}" && fail \
  "manifest public-key path contains a control character"
has_control_character "${RESTORE_ATTESTATION_VERIFY_KEY_FILE}" && fail \
  "restore attestation public-key path contains a control character"
[[ "${BACKUP_ROOT}" == /* ]] || fail "--backup must be an absolute path"
[[ "${REPORT_ROOT}" == /* ]] || fail "--report-root must be an absolute path"
[[ "${MANIFEST_VERIFY_KEY_FILE}" == /* && \
  "${RESTORE_ATTESTATION_VERIFY_KEY_FILE}" == /* ]] || fail \
  "backup and restore trust-key paths must be absolute"
[[ -f "${MANIFEST_VERIFY_KEY_FILE}" && ! -L "${MANIFEST_VERIFY_KEY_FILE}" ]] || fail \
  "backup manifest public trust-key file is missing or unsafe"
[[ -f "${RESTORE_ATTESTATION_VERIFY_KEY_FILE}" && \
  ! -L "${RESTORE_ATTESTATION_VERIFY_KEY_FILE}" ]] || fail \
  "restore attestation public trust-key file is missing or unsafe"
MANIFEST_VERIFY_KEY_FILE="$(cd "$(dirname "${MANIFEST_VERIFY_KEY_FILE}")" && pwd -P)/$(basename "${MANIFEST_VERIFY_KEY_FILE}")"
RESTORE_ATTESTATION_VERIFY_KEY_FILE="$(cd "$(dirname "${RESTORE_ATTESTATION_VERIFY_KEY_FILE}")" && pwd -P)/$(basename "${RESTORE_ATTESTATION_VERIFY_KEY_FILE}")"
"${PYTHON}" "${BACKUP_TOOLS}/manifest.py" key-id \
  --public-key "${MANIFEST_VERIFY_KEY_FILE}" >/dev/null || fail \
  "backup manifest Ed25519 trust anchor is invalid"
[[ "${QDRANT_MODE}" == "snapshot" || "${QDRANT_MODE}" == "rebuild-required" ]] || fail \
  "--qdrant-mode must be snapshot or rebuild-required"
if [[ -n "${ALLOW_RELEASE_MIGRATION_FROM}" && \
  ! "${ALLOW_RELEASE_MIGRATION_FROM}" =~ ^[0-9a-f]{40}$ && \
  ! "${ALLOW_RELEASE_MIGRATION_FROM}" =~ ^[0-9a-f]{64}$ ]]; then
  fail "--allow-release-migration-from must be a complete lowercase Git id"
fi
[[ -d "${BACKUP_ROOT}" && ! -L "${BACKUP_ROOT}" ]] || fail \
  "backup root must be a real directory, not a symlink"
BACKUP_ROOT="$(cd "${BACKUP_ROOT}" && pwd -P)"
paths_overlap "${MANIFEST_VERIFY_KEY_FILE}" "${BACKUP_ROOT}" && fail \
  "backup manifest public trust key must be external to the backup"
paths_overlap "${RESTORE_ATTESTATION_VERIFY_KEY_FILE}" "${BACKUP_ROOT}" && fail \
  "restore attestation public trust key must be external to the backup"
paths_overlap "${BACKUP_ROOT}" "${REPOSITORY_ROOT}" && fail \
  "backup path must not be an ancestor or descendant of the release bundle"
[[ -f "${ENV_FILE}" && ! -L "${ENV_FILE}" ]] || fail "Compose env file is missing or unsafe"
mkdir -p "${REPORT_ROOT}"
[[ -d "${REPORT_ROOT}" && ! -L "${REPORT_ROOT}" ]] || fail "report root must be a real directory"
REPORT_ROOT="$(cd "${REPORT_ROOT}" && pwd -P)"
paths_overlap "${REPORT_ROOT}" "${REPOSITORY_ROOT}" && fail \
  "report root must not be an ancestor or descendant of the release bundle"
paths_overlap "${REPORT_ROOT}" "${BACKUP_ROOT}" && fail \
  "report root and backup root must not contain one another"

RESTORE_STEP="snapshot-backup"
SOURCE_BACKUP_ROOT="${BACKUP_ROOT}"
RESTORE_SNAPSHOT_ROOT="$(mktemp -d "${REPORT_ROOT}/auris-flow-restore-snapshot.XXXXXX")"
"${PYTHON}" "${BACKUP_TOOLS}/manifest.py" snapshot \
  --source "${SOURCE_BACKUP_ROOT}" \
  --snapshot-root "${RESTORE_SNAPSHOT_ROOT}" \
  --public-key "${MANIFEST_VERIFY_KEY_FILE}" >/dev/null || fail \
  "could not create an isolated no-follow backup snapshot"
BACKUP_ROOT="${RESTORE_SNAPSHOT_ROOT}/backup"

RESTORE_STEP="verify-backup-snapshot"
verified_manifest_json="$("${PYTHON}" "${BACKUP_TOOLS}/manifest.py" verify \
  --root "${BACKUP_ROOT}" \
  --public-key "${MANIFEST_VERIFY_KEY_FILE}")" || fail \
  "external manifest signature or artifact verification failed"
gzip -t "${BACKUP_ROOT}/mysql/all-databases.sql.gz" || fail "MySQL dump is corrupt"
backup_id="$(printf '%s' "${verified_manifest_json}" | "${PYTHON}" -c \
  'import json,sys; print(json.load(sys.stdin)["backup_id"])')"
backup_commit="$(printf '%s' "${verified_manifest_json}" | "${PYTHON}" -c \
  'import json,sys; print(json.load(sys.stdin)["git_commit"])')"
backup_release="$(printf '%s' "${verified_manifest_json}" | "${PYTHON}" -c \
  'import json,sys; print(json.load(sys.stdin)["release_version"])')"
backup_metadata_sha256="$(printf '%s' "${verified_manifest_json}" | "${PYTHON}" -c \
  'import json,sys; print(json.load(sys.stdin)["release_metadata_sha256"])')"
backup_created_at_utc="$(printf '%s' "${verified_manifest_json}" | "${PYTHON}" -c \
  'import json,sys; print(json.load(sys.stdin)["created_at_utc"])')"
backup_manifest_sha256="$(printf '%s' "${verified_manifest_json}" | "${PYTHON}" -c \
  'import json,sys; print(json.load(sys.stdin)["manifest_sha256"])')"
backup_signing_key_id="$(printf '%s' "${verified_manifest_json}" | "${PYTHON}" -c \
  'import json,sys; print(json.load(sys.stdin)["signing_key_id"])')"
restore_attestation_key_id="$(printf '%s' "${verified_manifest_json}" | "${PYTHON}" -c \
  'import json,sys; print(json.load(sys.stdin)["restore_attestation_key_id"])')"
provided_restore_attestation_key_id="$("${PYTHON}" "${BACKUP_TOOLS}/manifest.py" key-id \
  --public-key "${RESTORE_ATTESTATION_VERIFY_KEY_FILE}" | "${PYTHON}" -c \
  'import json,sys; print(json.load(sys.stdin)["key_id"])')" || fail \
  "restore attestation public key is not a valid Ed25519 key"
[[ "${provided_restore_attestation_key_id}" == "${restore_attestation_key_id}" ]] || fail \
  "restore attestation public key does not match the signed manifest delegation"
[[ "${backup_signing_key_id}" != "${restore_attestation_key_id}" ]] || fail \
  "manifest signing and restore attestation key roles are not separated"
[[ "${CONFIRMATION}" == "${backup_id}" ]] || fail \
  "confirmation must exactly equal backup id ${backup_id}"
release_identity="$("${PYTHON}" "${RELEASE_BUNDLE_TOOL}" identity \
  --bundle-root "${REPOSITORY_ROOT}" \
  --verify-signature)" || fail "signed release metadata verification failed"
IFS=$'\t' read -r current_commit current_release current_metadata_sha256 \
  current_compose_sha256 current_image_lock_sha256 <<<"${release_identity}"
for digest in "${current_metadata_sha256}" "${current_compose_sha256}" \
  "${current_image_lock_sha256}" "${backup_metadata_sha256}"; do
  [[ "${digest}" =~ ^[0-9a-f]{64}$ ]] || fail "release metadata checksum is invalid"
done
if [[ "${current_commit}" == "${backup_commit}" && \
  "${current_release}" == "${backup_release}" && \
  "${current_metadata_sha256}" == "${backup_metadata_sha256}" ]]; then
  [[ -z "${ALLOW_RELEASE_MIGRATION_FROM}" ]] || fail \
    "release migration acknowledgement is not allowed when releases already match"
else
  [[ "${ALLOW_RELEASE_MIGRATION_FROM}" == "${backup_commit}" ]] || fail \
    "backup release differs from the installed release; pass --allow-release-migration-from ${backup_commit} only for a signed compatible predecessor"
  "${PYTHON}" "${RELEASE_BUNDLE_TOOL}" verify-restore-source \
    --bundle-root "${REPOSITORY_ROOT}" \
    --backup-release-tag "${backup_release}" \
    --backup-source-commit "${backup_commit}" \
    --backup-metadata-sha256 "${backup_metadata_sha256}" \
    --verify-signature \
    >/dev/null || fail \
    "backup release is outside the signed restore compatibility policy"
fi

REPORT_FILE="${REPORT_ROOT}/${backup_id}-$(date -u +%Y%m%dT%H%M%SZ).tsv"
printf 'timestamp_utc\tstatus\tstep\tmessage\n' >"${REPORT_FILE}"
write_report started "checksum verified and operator confirmation accepted"

compose() {
  COMPOSE_PROJECT_NAME="${PRODUCTION_PROJECT_NAME}" \
    docker --context "${DOCKER_CONTEXT_NAME}" compose \
    --project-name "${PRODUCTION_PROJECT_NAME}" \
    --project-directory "${PRODUCTION_ROOT}" \
    --env-file "${ENV_FILE}" \
    -f "${COMPOSE_FILE}" "$@"
}

minio_mc() {
  compose run --rm --no-deps -T \
    --entrypoint /opt/auris/minio-client.sh \
    minio-bootstrap "$@"
}

RESTORE_STEP="compose-preflight"
compose config --quiet || fail "Compose configuration is invalid"
running_services="$(compose ps --status running --services)"
for required_service in mysql minio qdrant redis; do
  if ! printf '%s\n' "${running_services}" | grep -Fxq "${required_service}"; then
    fail "required dependency service is not running: ${required_service}"
  fi
done
for writer_service in edge bff worker keycloak dagster-code dagster-webserver dagster-daemon migrate db-bootstrap minio-volume-init minio-bootstrap identity-bootstrap; do
  if printf '%s\n' "${running_services}" | grep -Fxq "${writer_service}"; then
    fail "writer service ${writer_service} is running; restore requires an offline application tier"
  fi
done
"${PYTHON}" "${RELEASE_BUNDLE_TOOL}" verify-running-images \
  --bundle-root "${REPOSITORY_ROOT}" \
  --project-directory "${PRODUCTION_ROOT}" \
  --env-file "${ENV_FILE}" \
  --project-name "${PRODUCTION_PROJECT_NAME}" \
  --docker-context "${DOCKER_CONTEXT_NAME}" \
  --service mysql \
  --service minio \
  --service qdrant \
  --service redis \
  --all-running-release-services \
  --verify-signature \
  >/dev/null || fail \
  "running release-service images do not match signed release metadata"
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
minio_mc ls --recursive --versions --json auris/auris-flow \
  >"${target_minio_listing}"
if [[ -s "${target_minio_listing}" ]]; then
  fail "target MinIO bucket contains object versions; refusing overwrite"
fi
compose run --rm --no-deps \
  --user "$(id -u):$(id -g)" \
  -v "${BACKUP_TOOLS}/qdrant_snapshots.py:/opt/auris/qdrant-snapshots.py:ro" \
  --entrypoint python qdrant-backup-tool /opt/auris/qdrant-snapshots.py assert-empty >/dev/null || fail \
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
"${PYTHON}" "${BACKUP_TOOLS}/minio_versions.py" verify-artifacts \
  --plan "${BACKUP_ROOT}/minio/versions.json" \
  --backup-root "${BACKUP_ROOT}" >/dev/null || fail \
  "MinIO backup generations do not match their bound SHA-256 values"
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
qdrant_rebuild_pending=false
if [[ "${QDRANT_MODE}" == "snapshot" ]]; then
  compose run --rm --no-deps \
    --user "$(id -u):$(id -g)" \
    -v "${BACKUP_ROOT}:/backup:ro" \
    -v "${BACKUP_TOOLS}/qdrant_snapshots.py:/opt/auris/qdrant-snapshots.py:ro" \
    --entrypoint python qdrant-backup-tool \
    /opt/auris/qdrant-snapshots.py restore --input /backup/qdrant || fail \
    "Qdrant derived snapshot restore failed; MySQL and MinIO remain authoritative"
  write_report passed "derived Qdrant snapshots restored after authoritative sources"
else
  qdrant_rebuild_pending=true
  write_report "pending-qdrant-rebuild" \
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
minio_mc ls --recursive --versions --json auris/auris-flow \
  >"${restored_minio_listing}"
"${PYTHON}" "${BACKUP_TOOLS}/minio_versions.py" compare-listing \
  --plan "${BACKUP_ROOT}/minio/versions.json" \
  --listing "${restored_minio_listing}" >/dev/null || fail \
  "restored MinIO semantic version history differs from backup"
restored_minio_verify_script="$(mktemp "${REPORT_ROOT}/restored-minio-verify.XXXXXX")"
"${PYTHON}" "${BACKUP_TOOLS}/minio_versions.py" emit-verify-shell \
  --plan "${BACKUP_ROOT}/minio/versions.json" \
  --listing "${restored_minio_listing}" \
  --output "${restored_minio_verify_script}" || fail \
  "could not map restored MinIO generations to target version ids"
compose run --rm --no-deps \
  --user "$(id -u):$(id -g)" \
  -v "${restored_minio_verify_script}:/opt/auris/minio-verify.sh:ro" \
  --entrypoint /bin/sh minio-bootstrap /opt/auris/minio-verify.sh || fail \
  "restored MinIO generation content failed SHA-256 or size verification"
if [[ "${QDRANT_MODE}" == "snapshot" ]]; then
  compose run --rm --no-deps \
    --user "$(id -u):$(id -g)" \
    -v "${BACKUP_ROOT}:/backup:ro" \
    -v "${BACKUP_TOOLS}/qdrant_snapshots.py:/opt/auris/qdrant-snapshots.py:ro" \
    --entrypoint python qdrant-backup-tool \
    /opt/auris/qdrant-snapshots.py verify-semantics --input /backup/qdrant >/dev/null || fail \
    "restored Qdrant full fingerprints or scoped probe query differ from backup"
  write_report passed \
    "authority counts, MinIO generation hashes, and Qdrant semantic fingerprints are consistent"
else
  write_report passed \
    "MySQL authority counts and MinIO generation hashes are consistent; Qdrant remains pending"
fi
rm -f \
  "${target_counts}" \
  "${target_minio_listing}" \
  "${minio_restore_script}" \
  "${restored_counts}" \
  "${restored_minio_listing}" \
  "${restored_minio_verify_script}"
if [[ "${qdrant_rebuild_pending}" == true ]]; then
  RESTORE_STEP="pending-qdrant-rebuild"
  RESTORE_STATE_FILE="${REPORT_FILE%.tsv}.state.json"
  set +e
  "${PYTHON}" "${RESTORE_STATE_TOOL}" create-pending \
    --output "${RESTORE_STATE_FILE}" \
    --backup-id "${backup_id}" \
    --backup-created-at-utc "${backup_created_at_utc}" \
    --source-commit "${backup_commit}" \
    --manifest-sha256 "${backup_manifest_sha256}" \
    --manifest-signing-key-id "${backup_signing_key_id}" \
    --attestation-key-id "${restore_attestation_key_id}" \
    --pending-at-utc "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >/dev/null
  pending_state_exit_code=$?
  set -e
  [[ "${pending_state_exit_code}" -eq "${RESTORE_PENDING_EXIT_CODE}" ]] || fail \
    "could not publish pending Qdrant rebuild state"
  write_report "pending-qdrant-rebuild" \
    "authoritative restore verified; governed Qdrant rebuild finalization is required"
  printf 'Authoritative restore verified for %s, but Qdrant rebuild is pending.\n' "${backup_id}"
  printf 'Pending state: %s\n' "${RESTORE_STATE_FILE}"
  printf 'Run production/scripts/finalize-restore.sh after governed rebuild jobs complete.\n'
  exit "${RESTORE_PENDING_EXIT_CODE}"
fi

RESTORE_STEP="complete"
write_report complete \
  "Redis cache intentionally omitted; run migrations for the selected release, then start application services"
printf 'Restore complete for %s. Report: %s\n' "${backup_id}" "${REPORT_FILE}"
printf 'Redis was not restored because it is a disposable cache, never a business authority.\n'
