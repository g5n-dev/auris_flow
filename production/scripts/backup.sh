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
OUTPUT_ROOT="${AURIS_BACKUP_OUTPUT_ROOT:-}"
RELEASE_VERSION="${AURIS_RELEASE_VERSION:-v0.0.0-dev}"
RUNTIME_METRICS_DIR="${AURIS_RUNTIME_METRICS_DIR:-${PRODUCTION_ROOT}/runtime-metrics}"
STORAGE_BOUNDARY=""
INCLUDE_REDIS=false
STAGING_DIR=""

usage() {
  cat <<'USAGE'
Usage: production/scripts/backup.sh --output-root ABSOLUTE_DIR \
       --storage-boundary encrypted-external [options]

Options:
  --env-file FILE             Compose environment file (default: production/.env)
  --release-version VERSION   Release represented by the backup
  --include-redis             Include a non-authoritative Redis RDB for diagnostics
  -h, --help                  Show this help

The application writer services must already be stopped. The output root must
be encrypted at rest and copied to an independent host/object store.
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
    --include-redis)
      INCLUDE_REDIS=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *) fail "unknown option: $1" ;;
  esac
done

for command_name in docker git gzip df awk mktemp "${PYTHON}"; do
  command -v "${command_name}" >/dev/null 2>&1 || fail "required command not found: ${command_name}"
done
[[ -f "${COMPOSE_FILE}" && ! -L "${COMPOSE_FILE}" ]] || fail "Compose file is missing or unsafe"
has_control_character "${OUTPUT_ROOT}" && fail "output root contains a control character"
has_control_character "${ENV_FILE}" && fail "Compose env path contains a control character"
[[ -f "${ENV_FILE}" && ! -L "${ENV_FILE}" ]] || fail "Compose env file is missing or unsafe: ${ENV_FILE}"
[[ "${OUTPUT_ROOT}" == /* ]] || fail "--output-root must be an absolute path"
[[ "${STORAGE_BOUNDARY}" == "encrypted-external" ]] || fail \
  "--storage-boundary must explicitly be encrypted-external"
[[ "${RELEASE_VERSION}" =~ ^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$ ]] || fail \
  "release version contains unsupported characters"

mkdir -p "${OUTPUT_ROOT}"
[[ -d "${OUTPUT_ROOT}" && ! -L "${OUTPUT_ROOT}" ]] || fail "output root must be a real directory"
OUTPUT_ROOT="$(cd "${OUTPUT_ROOT}" && pwd -P)"
case "${OUTPUT_ROOT}" in
  /|"${REPOSITORY_ROOT}"|"${PRODUCTION_ROOT}")
    fail "output root is too broad or overlaps the source repository"
    ;;
esac

compose() {
  docker compose \
    --project-directory "${PRODUCTION_ROOT}" \
    --env-file "${ENV_FILE}" \
    -f "${COMPOSE_FILE}" "$@"
}

compose config --quiet || fail "Compose configuration is invalid"
running_services="$(compose ps --status running --services)"
for required_service in mysql minio qdrant redis; do
  if ! printf '%s\n' "${running_services}" | grep -Fxq "${required_service}"; then
    fail "required dependency service is not running: ${required_service}"
  fi
done
for writer_service in edge bff worker keycloak dagster-code dagster-webserver dagster-daemon migrate db-bootstrap; do
  if printf '%s\n' "${running_services}" | grep -Fxq "${writer_service}"; then
    fail "writer service ${writer_service} is running; enter the documented backup maintenance window first"
  fi
done

mysql_bytes="$(compose exec -T mysql sh -c '
  export MYSQL_PWD="$(cat /run/secrets/mysql_root_password)"
  exec mysql --protocol=tcp --host=127.0.0.1 --user=root --batch --skip-column-names \
    -e "SELECT COALESCE(SUM(DATA_LENGTH + INDEX_LENGTH), 0) FROM information_schema.TABLES WHERE TABLE_SCHEMA IN (\"auris_flow\", \"keycloak\", \"dagster\")"
')"
minio_bytes="$(compose exec -T minio mc du --versions --json local/auris-flow | \
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

source_commit="${AURIS_SOURCE_COMMIT:-$(git -C "${REPOSITORY_ROOT}" rev-parse HEAD)}"
[[ "${source_commit}" =~ ^[0-9a-f]{40}$ || "${source_commit}" =~ ^[0-9a-f]{64}$ ]] || fail \
  "source commit is unavailable or invalid"
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
compose exec -T minio mc ls --recursive --versions --json local/auris-flow \
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

printf 'Capturing Qdrant derived-index snapshots...\n'
compose run --rm --no-deps \
  --user "$(id -u):$(id -g)" \
  -v "${STAGING_DIR}:/backup" \
  -v "${BACKUP_TOOLS}/qdrant_snapshots.py:/opt/auris/qdrant-snapshots.py:ro" \
  --entrypoint python bff \
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
  printf 'minio-client\t%s\n' "$(compose exec -T minio mc --version | head -n 1)"
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
  --tool-versions "${STAGING_DIR}/metadata/tool-versions.json"
"${PYTHON}" "${BACKUP_TOOLS}/manifest.py" verify --root "${STAGING_DIR}"
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
printf 'Copy this directory to an independent encrypted store before leaving the maintenance window.\n'
