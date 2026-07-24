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
RELEASE_BUNDLE_TOOL="${REPOSITORY_ROOT}/scripts/release_bundle.py"
RELEASE_METADATA_FILE="${PRODUCTION_ROOT}/release-metadata.json"
RELEASE_METADATA_SIGNATURE="${PRODUCTION_ROOT}/release-metadata.sigstore.json"
PYTHON="${PYTHON:-python3}"
ENV_FILE="${AURIS_COMPOSE_ENV_FILE:-${PRODUCTION_ROOT}/.env}"
OUTPUT_ROOT="${AURIS_BACKUP_OUTPUT_ROOT:-}"
RELEASE_VERSION="${AURIS_RELEASE_VERSION:-}"
RUNTIME_METRICS_DIR="${AURIS_RUNTIME_METRICS_DIR:-${PRODUCTION_ROOT}/runtime-metrics}"
SECRETS_DIR="${AURIS_SECRETS_DIR:-${PRODUCTION_ROOT}/secrets}"
MANIFEST_SIGNING_PRIVATE_KEY_FILE="${AURIS_BACKUP_MANIFEST_SIGNING_PRIVATE_KEY_FILE:-${SECRETS_DIR}/backup_manifest_signing_private_key.pem}"
MANIFEST_VERIFY_KEY_FILE="${AURIS_BACKUP_MANIFEST_VERIFY_KEY_FILE:-${SECRETS_DIR}/backup_manifest_signing_public_key.pem}"
RESTORE_ATTESTATION_VERIFY_KEY_FILE="${AURIS_RESTORE_ATTESTATION_VERIFY_KEY_FILE:-${SECRETS_DIR}/restore_attestation_signing_public_key.pem}"
STORAGE_BOUNDARY=""
RELEASE_GATE_DRILL=false
INCLUDE_REDIS=false
STAGING_DIR=""

usage() {
  cat <<'USAGE'
Usage: production/scripts/backup.sh --output-root ABSOLUTE_DIR \
       --storage-boundary MODE [options]

Options:
  --env-file FILE             Compose environment file (default: production/.env)
  --release-version VERSION   Release represented by the backup
  --manifest-private-key FILE Deployment-owned Ed25519 private-key secret file
  --manifest-public-key FILE  Deployment-owned Ed25519 trust-anchor public key
  --restore-attestation-public-key FILE
                              Deployment-owned restore-attestation public key
  --release-gate-drill        Permit the non-retained ephemeral-ci-drill mode
                              for synthetic release recovery verification only
  --include-redis             Include a non-authoritative Redis RDB for diagnostics
  -h, --help                  Show this help

The application writer services must already be stopped. Production backups
use encrypted-external storage. ephemeral-ci-drill is a non-retained synthetic
release recovery test and additionally requires --release-gate-drill.
USAGE
}

fail() {
  printf 'backup failed: %s\n' "$1" >&2
  if [[ -n "${STAGING_DIR}" ]]; then
    printf 'incomplete staging directory retained for diagnosis: %s\n' "${STAGING_DIR}" >&2
  fi
  exit 2
}

has_control_character() {
  [[ "$1" == *$'\n'* || "$1" == *$'\r'* || "$1" == *$'\t'* ]]
}

paths_overlap() {
  local first="${1%/}/" second="${2%/}/"
  [[ "${first}" == "${second}"* || "${second}" == "${first}"* ]]
}

while (($#)); do
  case "$1" in
    --output-root)
      (($# >= 2)) || fail "--output-root requires a value"
      OUTPUT_ROOT="$2"
      shift 2
      ;;
    --storage-boundary)
      (($# >= 2)) || fail "--storage-boundary requires a value"
      STORAGE_BOUNDARY="$2"
      shift 2
      ;;
    --env-file)
      (($# >= 2)) || fail "--env-file requires a value"
      ENV_FILE="$2"
      shift 2
      ;;
    --release-version)
      (($# >= 2)) || fail "--release-version requires a value"
      RELEASE_VERSION="$2"
      shift 2
      ;;
    --manifest-private-key)
      (($# >= 2)) || fail "--manifest-private-key requires a value"
      MANIFEST_SIGNING_PRIVATE_KEY_FILE="$2"
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
    --include-redis)
      INCLUDE_REDIS=true
      shift
      ;;
    --release-gate-drill)
      RELEASE_GATE_DRILL=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *) fail "unknown option: $1" ;;
  esac
done

for command_name in docker gzip df awk mktemp install cosign openssl "${PYTHON}"; do
  command -v "${command_name}" >/dev/null 2>&1 || fail "required command not found: ${command_name}"
done
[[ -f "${COMPOSE_FILE}" && ! -L "${COMPOSE_FILE}" ]] || fail "Compose file is missing or unsafe"
[[ -f "${RELEASE_BUNDLE_TOOL}" && ! -L "${RELEASE_BUNDLE_TOOL}" ]] || fail \
  "release bundle verifier is missing or unsafe"
[[ -f "${RELEASE_METADATA_FILE}" && ! -L "${RELEASE_METADATA_FILE}" ]] || fail \
  "signed release metadata is missing or unsafe"
[[ -f "${RELEASE_METADATA_SIGNATURE}" && ! -L "${RELEASE_METADATA_SIGNATURE}" ]] || fail \
  "release metadata Sigstore bundle is missing or unsafe"
[[ -z "${AURIS_SOURCE_COMMIT:-}" ]] || fail \
  "AURIS_SOURCE_COMMIT is forbidden; source identity comes from signed release metadata"
[[ -z "${DOCKER_HOST:-}" && -z "${DOCKER_CONTEXT:-}" && \
  -z "${COMPOSE_PROJECT_NAME:-}" ]] || fail \
  "DOCKER_HOST, DOCKER_CONTEXT and COMPOSE_PROJECT_NAME overrides are forbidden"
docker --context "${DOCKER_CONTEXT_NAME}" info >/dev/null 2>&1 || fail \
  "the bound Docker context is unavailable: ${DOCKER_CONTEXT_NAME}"
has_control_character "${OUTPUT_ROOT}" && fail "output root contains a control character"
has_control_character "${ENV_FILE}" && fail "Compose env path contains a control character"
has_control_character "${MANIFEST_SIGNING_PRIVATE_KEY_FILE}" && fail \
  "manifest private-key path contains a control character"
has_control_character "${MANIFEST_VERIFY_KEY_FILE}" && fail \
  "manifest public-key path contains a control character"
has_control_character "${RESTORE_ATTESTATION_VERIFY_KEY_FILE}" && fail \
  "restore attestation public-key path contains a control character"
[[ -f "${ENV_FILE}" && ! -L "${ENV_FILE}" ]] || fail "Compose env file is missing or unsafe: ${ENV_FILE}"
[[ "${OUTPUT_ROOT}" == /* ]] || fail "--output-root must be an absolute path"
[[ "${MANIFEST_SIGNING_PRIVATE_KEY_FILE}" == /* && \
  "${MANIFEST_VERIFY_KEY_FILE}" == /* && \
  "${RESTORE_ATTESTATION_VERIFY_KEY_FILE}" == /* ]] || fail \
  "backup manifest signing key paths must be absolute"
[[ -f "${MANIFEST_SIGNING_PRIVATE_KEY_FILE}" && \
  ! -L "${MANIFEST_SIGNING_PRIVATE_KEY_FILE}" ]] || fail \
  "backup manifest private-key secret file is missing or unsafe"
[[ -f "${MANIFEST_VERIFY_KEY_FILE}" && ! -L "${MANIFEST_VERIFY_KEY_FILE}" ]] || fail \
  "backup manifest public trust-key file is missing or unsafe"
[[ -f "${RESTORE_ATTESTATION_VERIFY_KEY_FILE}" && \
  ! -L "${RESTORE_ATTESTATION_VERIFY_KEY_FILE}" ]] || fail \
  "restore attestation public trust-key file is missing or unsafe"
MANIFEST_SIGNING_PRIVATE_KEY_FILE="$(cd "$(dirname "${MANIFEST_SIGNING_PRIVATE_KEY_FILE}")" && pwd -P)/$(basename "${MANIFEST_SIGNING_PRIVATE_KEY_FILE}")"
MANIFEST_VERIFY_KEY_FILE="$(cd "$(dirname "${MANIFEST_VERIFY_KEY_FILE}")" && pwd -P)/$(basename "${MANIFEST_VERIFY_KEY_FILE}")"
RESTORE_ATTESTATION_VERIFY_KEY_FILE="$(cd "$(dirname "${RESTORE_ATTESTATION_VERIFY_KEY_FILE}")" && pwd -P)/$(basename "${RESTORE_ATTESTATION_VERIFY_KEY_FILE}")"
[[ "${MANIFEST_SIGNING_PRIVATE_KEY_FILE}" != "${MANIFEST_VERIFY_KEY_FILE}" ]] || fail \
  "backup manifest private and public keys must use different files"
manifest_key_json="$("${PYTHON}" "${BACKUP_TOOLS}/manifest.py" verify-key-pair \
  --private-key "${MANIFEST_SIGNING_PRIVATE_KEY_FILE}" \
  --public-key "${MANIFEST_VERIFY_KEY_FILE}")" || fail \
  "backup manifest Ed25519 signing key pair is invalid"
restore_attestation_key_json="$("${PYTHON}" "${BACKUP_TOOLS}/manifest.py" key-id \
  --public-key "${RESTORE_ATTESTATION_VERIFY_KEY_FILE}")" || fail \
  "restore attestation public key is not a valid Ed25519 key"
manifest_signing_key_id="$(printf '%s' "${manifest_key_json}" | "${PYTHON}" -c \
  'import json,sys; print(json.load(sys.stdin)["key_id"])')"
restore_attestation_key_id="$(printf '%s' "${restore_attestation_key_json}" | "${PYTHON}" -c \
  'import json,sys; print(json.load(sys.stdin)["key_id"])')"
[[ "${manifest_signing_key_id}" != "${restore_attestation_key_id}" ]] || fail \
  "backup manifest and restore attestation must use distinct Ed25519 keys"
case "${STORAGE_BOUNDARY}" in
  encrypted-external)
    [[ "${RELEASE_GATE_DRILL}" == false ]] || fail \
      "--release-gate-drill requires --storage-boundary ephemeral-ci-drill"
    ;;
  ephemeral-ci-drill)
    [[ "${RELEASE_GATE_DRILL}" == true ]] || fail \
      "ephemeral-ci-drill requires the explicit --release-gate-drill flag"
    ;;
  *)
    fail "--storage-boundary must be encrypted-external or ephemeral-ci-drill"
    ;;
esac
release_identity="$("${PYTHON}" "${RELEASE_BUNDLE_TOOL}" identity \
  --bundle-root "${REPOSITORY_ROOT}" \
  --verify-signature)" || fail "signed release metadata verification failed"
IFS=$'\t' read -r source_commit metadata_release_version release_metadata_sha256 \
  release_compose_sha256 release_image_lock_sha256 <<<"${release_identity}"
[[ "${source_commit}" =~ ^[0-9a-f]{40}$ || "${source_commit}" =~ ^[0-9a-f]{64}$ ]] || fail \
  "release metadata source commit is invalid"
[[ "${release_metadata_sha256}" =~ ^[0-9a-f]{64}$ ]] || fail \
  "release metadata checksum is invalid"
[[ "${release_compose_sha256}" =~ ^[0-9a-f]{64}$ ]] || fail \
  "release Compose checksum is invalid"
[[ "${release_image_lock_sha256}" =~ ^[0-9a-f]{64}$ ]] || fail \
  "release image-lock checksum is invalid"
if [[ -n "${RELEASE_VERSION}" && "${RELEASE_VERSION}" != "${metadata_release_version}" ]]; then
  fail "--release-version/AURIS_RELEASE_VERSION does not match signed release metadata"
fi
RELEASE_VERSION="${metadata_release_version}"

mkdir -p "${OUTPUT_ROOT}"
[[ -d "${OUTPUT_ROOT}" && ! -L "${OUTPUT_ROOT}" ]] || fail "output root must be a real directory"
OUTPUT_ROOT="$(cd "${OUTPUT_ROOT}" && pwd -P)"
paths_overlap "${OUTPUT_ROOT}" "${REPOSITORY_ROOT}" && fail \
  "output root must not be an ancestor or descendant of the release bundle"
paths_overlap "${MANIFEST_SIGNING_PRIVATE_KEY_FILE}" "${OUTPUT_ROOT}" && fail \
  "backup manifest private key must be external to the backup output root"
paths_overlap "${MANIFEST_VERIFY_KEY_FILE}" "${OUTPUT_ROOT}" && fail \
  "backup manifest public key must be external to the backup output root"
paths_overlap "${RESTORE_ATTESTATION_VERIFY_KEY_FILE}" "${OUTPUT_ROOT}" && fail \
  "restore attestation public key must be external to the backup output root"

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

compose config --quiet || fail "Compose configuration is invalid"
running_services="$(compose ps --status running --services)"
for required_service in mysql minio qdrant redis; do
  if ! printf '%s\n' "${running_services}" | grep -Fxq "${required_service}"; then
    fail "required dependency service is not running: ${required_service}"
  fi
done
for writer_service in edge bff worker keycloak dagster-code dagster-webserver dagster-daemon migrate dagster-storage-bootstrap db-bootstrap minio-volume-init minio-bootstrap identity-bootstrap; do
  if printf '%s\n' "${running_services}" | grep -Fxq "${writer_service}"; then
    fail "writer service ${writer_service} is running; enter the documented backup maintenance window first"
  fi
done

mysql_bytes="$(compose exec -T mysql sh -c '
  export MYSQL_PWD="$(cat /run/secrets/mysql_root_password)"
  exec mysql --protocol=tcp --host=127.0.0.1 --user=root --batch --skip-column-names \
    -e "SELECT COALESCE(SUM(DATA_LENGTH + INDEX_LENGTH), 0) FROM information_schema.TABLES WHERE TABLE_SCHEMA IN (\"auris_flow\", \"keycloak\", \"dagster\")"
')"
minio_bytes="$(minio_mc du --versions --json auris/auris-flow | \
  "${PYTHON}" "${BACKUP_TOOLS}/minio_versions.py" du-size)"
qdrant_kib="$(compose exec -T qdrant du -sk /qdrant/storage | awk '{print $1}')"
redis_kib=0
if [[ "${INCLUDE_REDIS}" == true ]]; then
  redis_kib="$(compose exec -T redis du -sk /data | awk '{print $1}')"
fi
for numeric_value in "${mysql_bytes}" "${minio_bytes}" "${qdrant_kib}" "${redis_kib}"; do
  [[ "${numeric_value}" =~ ^[0-9]+$ ]] || fail "capacity estimate returned a non-integer value"
done
estimated_bytes=$((mysql_bytes + minio_bytes + ((qdrant_kib + redis_kib) * 1024)))
capacity_percent="${AURIS_BACKUP_CAPACITY_PERCENT:-200}"
minimum_free_bytes="${AURIS_BACKUP_MIN_FREE_BYTES:-1073741824}"
[[ "${capacity_percent}" =~ ^[0-9]+$ && "${minimum_free_bytes}" =~ ^[0-9]+$ ]] || fail \
  "backup capacity settings must be non-negative integers"
((capacity_percent >= 150)) || fail "backup capacity percent cannot be lower than 150"
((minimum_free_bytes >= 1073741824)) || fail \
  "backup minimum free space cannot be lower than 1 GiB"
available_bytes="$(df -Pk "${OUTPUT_ROOT}" | awk 'NR == 2 { printf "%.0f", $4 * 1024 }')"
[[ "${available_bytes}" =~ ^[0-9]+$ ]] || fail "could not determine backup filesystem capacity"
required_bytes=$(((estimated_bytes * capacity_percent / 100) + minimum_free_bytes))
((available_bytes >= required_bytes)) || fail \
  "insufficient backup capacity: available=${available_bytes}, required=${required_bytes}"

created_at_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
compact_timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_id="auris-flow-${compact_timestamp}-${source_commit:0:12}"
final_dir="${OUTPUT_ROOT}/${backup_id}"
[[ ! -e "${final_dir}" ]] || fail "backup destination already exists: ${final_dir}"
STAGING_DIR="$(mktemp -d "${OUTPUT_ROOT}/.auris-flow-backup.XXXXXX")"
mkdir -p \
  "${STAGING_DIR}/mysql" \
  "${STAGING_DIR}/minio" \
  "${STAGING_DIR}/qdrant" \
  "${STAGING_DIR}/metadata"

install -m 0600 "${RELEASE_METADATA_FILE}" \
  "${STAGING_DIR}/metadata/release-metadata.json"
install -m 0600 "${RELEASE_METADATA_SIGNATURE}" \
  "${STAGING_DIR}/metadata/release-metadata.sigstore.json"
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
  >"${STAGING_DIR}/metadata/running-images.json" || fail \
  "running release-service images do not match signed release digests"

printf 'Creating a quiesced MySQL logical backup...\n'
compose exec -T mysql sh -c '
  export MYSQL_PWD="$(cat /run/secrets/mysql_root_password)"
  exec mysqldump --protocol=tcp --host=127.0.0.1 --user=root \
    --single-transaction --quick --routines --triggers --events \
    --hex-blob --default-character-set=utf8mb4 --no-tablespaces \
    --set-gtid-purged=OFF --add-drop-database \
    --databases auris_flow keycloak dagster
' | gzip -9 -n >"${STAGING_DIR}/mysql/all-databases.sql.gz"

compose exec -T mysql sh -c '
  export MYSQL_PWD="$(cat /run/secrets/mysql_root_password)"
  exec mysql --protocol=tcp --host=127.0.0.1 --user=root \
    --batch --raw --skip-column-names
' <"${BACKUP_TOOLS}/mysql_counts.sql" >"${STAGING_DIR}/mysql/table-counts.tsv"

printf 'Capturing every MinIO object generation and delete marker...\n'
minio_mc ls --recursive --versions --json auris/auris-flow \
  >"${STAGING_DIR}/minio/source-listing.jsonl"
"${PYTHON}" "${BACKUP_TOOLS}/minio_versions.py" plan \
  --listing "${STAGING_DIR}/minio/source-listing.jsonl" \
  --bucket auris-flow \
  --output "${STAGING_DIR}/minio/versions.json"
minio_backup_script="$(mktemp "${TMPDIR:-/tmp}/auris-flow-minio-backup.XXXXXX")"
"${PYTHON}" "${BACKUP_TOOLS}/minio_versions.py" emit-backup-shell \
  --plan "${STAGING_DIR}/minio/versions.json" \
  --output "${minio_backup_script}"
compose run --rm --no-deps \
  --user "$(id -u):$(id -g)" \
  -v "${STAGING_DIR}:/backup" \
  -v "${minio_backup_script}:/opt/auris/minio-backup.sh:ro" \
  --entrypoint /bin/sh minio-bootstrap /opt/auris/minio-backup.sh
rm -f "${minio_backup_script}"
"${PYTHON}" "${BACKUP_TOOLS}/minio_versions.py" bind-artifacts \
  --plan "${STAGING_DIR}/minio/versions.json" \
  --backup-root "${STAGING_DIR}" \
  --output "${STAGING_DIR}/minio/versions.json"
"${PYTHON}" "${BACKUP_TOOLS}/minio_versions.py" verify-artifacts \
  --plan "${STAGING_DIR}/minio/versions.json" \
  --backup-root "${STAGING_DIR}" >/dev/null

printf 'Capturing Qdrant derived-index snapshots...\n'
compose run --rm --no-deps \
  --user "$(id -u):$(id -g)" \
  -v "${STAGING_DIR}:/backup" \
  -v "${BACKUP_TOOLS}/qdrant_snapshots.py:/opt/auris/qdrant-snapshots.py:ro" \
  --entrypoint python qdrant-backup-tool \
  /opt/auris/qdrant-snapshots.py backup --output /backup/qdrant

if [[ "${INCLUDE_REDIS}" == true ]]; then
  printf 'Capturing optional, non-authoritative Redis RDB...\n'
  mkdir -p "${STAGING_DIR}/redis"
  compose run --rm --no-deps \
    --user "$(id -u):$(id -g)" \
    -v "${STAGING_DIR}/redis:/backup" \
    --entrypoint /bin/sh redis -c \
    'redis_url="$(cat /run/secrets/redis_url)"; redis_password="${redis_url#redis://default:}"; redis_password="${redis_password%@*}"; unset redis_url; export REDISCLI_AUTH="${redis_password}"; unset redis_password; exec redis-cli --user default --host redis --no-auth-warning --rdb /backup/cache.rdb'
fi

printf 'Recording tool and immutable image versions...\n'
compose images --format json >"${STAGING_DIR}/metadata/compose-images.jsonl"
{
  printf 'docker\t%s\n' "$(docker version --format '{{.Server.Version}}')"
  printf 'docker-compose\t%s\n' "$(docker compose version --short)"
  printf 'mysql-dump\t%s\n' "$(compose exec -T mysql mysqldump --version)"
  printf 'minio\t%s\n' "$(compose exec -T minio minio --version | head -n 1)"
  printf 'minio-client\t%s\n' "$(minio_mc --version | head -n 1)"
  printf 'qdrant\t%s\n' "$(compose exec -T qdrant /qdrant/qdrant --version | head -n 1)"
  printf 'redis\t%s\n' "$(compose exec -T redis redis-server --version | head -n 1)"
} >"${STAGING_DIR}/metadata/tool-versions.tsv"
"${PYTHON}" "${BACKUP_TOOLS}/manifest.py" build-tool-versions \
  --versions-tsv "${STAGING_DIR}/metadata/tool-versions.tsv" \
  --images-jsonl "${STAGING_DIR}/metadata/compose-images.jsonl" \
  --output "${STAGING_DIR}/metadata/tool-versions.json"

count_arguments=(
  build-counts
  --mysql-tsv "${STAGING_DIR}/mysql/table-counts.tsv"
  --minio-plan "${STAGING_DIR}/minio/versions.json"
  --qdrant-metadata "${STAGING_DIR}/qdrant/snapshots.json"
  --output "${STAGING_DIR}/metadata/counts.json"
)
if [[ "${INCLUDE_REDIS}" == true ]]; then
  count_arguments+=(--redis-included)
fi
"${PYTHON}" "${BACKUP_TOOLS}/manifest.py" "${count_arguments[@]}"
"${PYTHON}" "${BACKUP_TOOLS}/manifest.py" create \
  --root "${STAGING_DIR}" \
  --backup-id "${backup_id}" \
  --created-at-utc "${created_at_utc}" \
  --git-commit "${source_commit}" \
  --release-version "${RELEASE_VERSION}" \
  --counts "${STAGING_DIR}/metadata/counts.json" \
  --tool-versions "${STAGING_DIR}/metadata/tool-versions.json" \
  --release-metadata "${STAGING_DIR}/metadata/release-metadata.json" \
  --running-images "${STAGING_DIR}/metadata/running-images.json" \
  --storage-boundary "${STORAGE_BOUNDARY}" \
  --restore-attestation-public-key "${RESTORE_ATTESTATION_VERIFY_KEY_FILE}"
"${PYTHON}" "${BACKUP_TOOLS}/manifest.py" sign \
  --root "${STAGING_DIR}" \
  --private-key "${MANIFEST_SIGNING_PRIVATE_KEY_FILE}" \
  --public-key "${MANIFEST_VERIFY_KEY_FILE}" || fail \
  "backup manifest signature creation failed"
"${PYTHON}" "${BACKUP_TOOLS}/manifest.py" verify \
  --root "${STAGING_DIR}" \
  --public-key "${MANIFEST_VERIFY_KEY_FILE}" || fail \
  "signed backup manifest verification failed"
gzip -t "${STAGING_DIR}/mysql/all-databases.sql.gz"

mv "${STAGING_DIR}" "${final_dir}"
STAGING_DIR=""

publish_success_metric() {
  if [[ -L "${RUNTIME_METRICS_DIR}" ]]; then
    printf 'warning: backup succeeded but runtime metrics directory is a symlink; metric not published\n' >&2
    return 0
  fi
  mkdir -p "${RUNTIME_METRICS_DIR}" || return 0
  local metric_tmp
  metric_tmp="$(mktemp "${RUNTIME_METRICS_DIR}/.auris-backup-metric.XXXXXX")" || return 0
  {
    printf '# HELP auris_backup_last_success_timestamp_seconds Unix timestamp of the latest complete verified backup.\n'
    printf '# TYPE auris_backup_last_success_timestamp_seconds gauge\n'
    printf 'auris_backup_last_success_timestamp_seconds %s\n' "$(date -u +%s)"
  } >"${metric_tmp}"
  chmod 0644 "${metric_tmp}"
  mv "${metric_tmp}" "${RUNTIME_METRICS_DIR}/auris_backup.prom"
}

publish_success_metric
printf 'Backup complete: %s\n' "${final_dir}"
if [[ "${STORAGE_BOUNDARY}" == "encrypted-external" ]]; then
  printf 'Copy this directory to an independent encrypted store before leaving the maintenance window.\n'
else
  printf 'Ephemeral release recovery drill only; this backup is not retained or production-qualified.\n'
fi
