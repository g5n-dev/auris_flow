#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRETS_DIR="${AURIS_SECRETS_DIR:-${ROOT}/secrets}"
RUNTIME_METRICS_DIR="${AURIS_RUNTIME_METRICS_DIR:-${ROOT}/runtime-metrics}"
TENANT_ID="${AURIS_BOOTSTRAP_TENANT_ID:-aurora_auto}"
PROJECT_ID="${AURIS_BOOTSTRAP_PROJECT_ID:-sales_qa}"

if [[ ! "${TENANT_ID}" =~ ^[A-Za-z0-9._-]+$ || ! "${PROJECT_ID}" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "tenant/project identifiers may only contain letters, digits, dot, underscore and dash" >&2
  exit 2
fi

umask 077
mkdir -p "${SECRETS_DIR}"
chmod 700 "${SECRETS_DIR}"
if [[ -L "${RUNTIME_METRICS_DIR}" ]]; then
  echo "refusing symlink runtime metrics directory: ${RUNTIME_METRICS_DIR}" >&2
  exit 2
fi
mkdir -p "${RUNTIME_METRICS_DIR}"
chmod 755 "${RUNTIME_METRICS_DIR}"

random_hex() {
  openssl rand -hex "$1"
}

secret_value() {
  local name="$1" generated="$2" target="${SECRETS_DIR}/$1"
  if [[ -e "${target}" ]]; then
    if [[ ! -f "${target}" || -L "${target}" ]]; then
      echo "refusing non-regular secret path: ${target}" >&2
      exit 2
    fi
    echo "preserving existing secret: ${name}" >&2
    tr -d '\r\n' <"${target}"
    return 0
  fi
  printf '%s\n' "${generated}" >"${target}"
  chmod 600 "${target}"
  printf '%s' "${generated}"
}

write_once() {
  local name="$1" value="$2"
  secret_value "${name}" "${value}" >/dev/null
}

mysql_root="$(secret_value mysql_root_password "$(random_hex 32)")"
mysql_runtime="$(secret_value mysql_runtime_password "$(random_hex 32)")"
mysql_migration="$(secret_value mysql_migration_password "$(random_hex 32)")"
keycloak_db="$(secret_value keycloak_db_password "$(random_hex 32)")"
dagster_db="$(secret_value dagster_db_password "$(random_hex 32)")"
redis_password="$(random_hex 32)"
object_access="$(secret_value object_storage_access_key "auris$(random_hex 10)")"
object_secret="$(secret_value object_storage_secret_key "$(random_hex 32)")"
qdrant_key="$(secret_value qdrant_api_key "$(random_hex 32)")"
embedding_key="$(secret_value embedding_api_key "$(random_hex 32)")"
completion_key="$(random_hex 32)"
callback_key="$(random_hex 32)"

write_once keycloak_admin_user "auris-admin"
write_once keycloak_admin_password "$(random_hex 32)"
write_once keycloak_bootstrap_operator_password "$(random_hex 32)"
write_once grafana_admin_user "auris-observer"
write_once grafana_admin_password "$(random_hex 32)"
if [[ -f "${SECRETS_DIR}/redis_url" ]]; then
  redis_password="$(cut -d: -f3 "${SECRETS_DIR}/redis_url" | cut -d@ -f1)"
fi
if [[ -z "${redis_password}" ]]; then
  echo "existing redis_url has an invalid format" >&2
  exit 2
fi
write_once runtime_database_url "mysql+pymysql://auris_runtime:${mysql_runtime}@mysql:3306/auris_flow"
write_once migration_database_url "mysql+pymysql://auris_migration:${mysql_migration}@mysql:3306/auris_flow"
write_once dagster_database_url "mysql+pymysql://dagster:${dagster_db}@mysql:3306/dagster"
write_once redis_url "redis://default:${redis_password}@redis:6379/0"
write_once redis_users.acl "user default on >${redis_password} ~* +@all"
write_once audio_playback_grant_secret "$(random_hex 32)"
write_once experiment_assignment_secret "$(random_hex 32)"
write_once completion_receipt_key_bindings "{\"dagster-v1\":{\"secret\":\"${completion_key}\",\"allowed_sources\":[\"dagster\"],\"allowed_scopes\":[{\"tenant_id\":\"${TENANT_ID}\",\"project_id\":\"${PROJECT_ID}\"}]}}"
write_once external_callback_key_bindings "{\"callback-v1\":{\"secret\":\"${callback_key}\",\"state\":\"active\"}}"

echo "Secrets initialized in ${SECRETS_DIR}. Back them up in an external secret manager."
echo "TLS certificate/key are intentionally not generated; install them under production/tls."
