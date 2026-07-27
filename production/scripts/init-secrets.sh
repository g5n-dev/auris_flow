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
if [[ -L "${SECRETS_DIR}" ]]; then
  echo "refusing symlink secrets directory: ${SECRETS_DIR}" >&2
  exit 2
fi
mkdir -p "${SECRETS_DIR}"
if [[ ! -d "${SECRETS_DIR}" || -L "${SECRETS_DIR}" ]]; then
  echo "secrets path must be a real directory: ${SECRETS_DIR}" >&2
  exit 2
fi
SECRETS_DIR="$(cd -P "${SECRETS_DIR}" && pwd)"
chmod 700 "${SECRETS_DIR}"
if [[ -L "${RUNTIME_METRICS_DIR}" ]]; then
  echo "refusing symlink runtime metrics directory: ${RUNTIME_METRICS_DIR}" >&2
  exit 2
fi
mkdir -p "${RUNTIME_METRICS_DIR}"
if [[ ! -d "${RUNTIME_METRICS_DIR}" || -L "${RUNTIME_METRICS_DIR}" ]]; then
  echo "runtime metrics path must be a real directory: ${RUNTIME_METRICS_DIR}" >&2
  exit 2
fi
RUNTIME_METRICS_DIR="$(cd -P "${RUNTIME_METRICS_DIR}" && pwd)"
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
    chmod 444 "${target}"
    echo "preserving existing secret: ${name}" >&2
    tr -d '\r\n' <"${target}"
    return 0
  fi
  printf '%s\n' "${generated}" >"${target}"
  chmod 444 "${target}"
  printf '%s' "${generated}"
}

write_once() {
  local name="$1" value="$2"
  secret_value "${name}" "${value}" >/dev/null
}

ensure_ed25519_signing_keys() {
  local key_prefix="$1" key_label="$2" temporary_label="$3"
  local private_key="${SECRETS_DIR}/${key_prefix}_signing_private_key.pem"
  local public_key="${SECRETS_DIR}/${key_prefix}_signing_public_key.pem"
  local private_exists=false public_exists=false
  [[ -e "${private_key}" || -L "${private_key}" ]] && private_exists=true
  [[ -e "${public_key}" || -L "${public_key}" ]] && public_exists=true
  if [[ "${private_exists}" != "${public_exists}" ]]; then
    echo "${key_label} key pair must be provided together" >&2
    exit 2
  fi
  if [[ "${private_exists}" == true ]]; then
    for key_path in "${private_key}" "${public_key}"; do
      if [[ ! -f "${key_path}" || -L "${key_path}" ]]; then
        echo "refusing unsafe ${key_label} key: ${key_path}" >&2
        exit 2
      fi
    done
    echo "preserving existing ${key_label} key pair" >&2
  else
    local private_tmp public_tmp
    private_tmp="$(mktemp "${SECRETS_DIR}/.${temporary_label}-private.XXXXXX")"
    public_tmp="$(mktemp "${SECRETS_DIR}/.${temporary_label}-public.XXXXXX")"
    if ! openssl genpkey -algorithm ED25519 -out "${private_tmp}" >/dev/null 2>&1 || \
      ! openssl pkey -in "${private_tmp}" -pubout -out "${public_tmp}" >/dev/null 2>&1; then
      rm -f -- "${private_tmp}" "${public_tmp}"
      echo "could not generate Ed25519 ${key_label} key pair" >&2
      exit 2
    fi
    chmod 400 "${private_tmp}"
    chmod 444 "${public_tmp}"
    mv "${private_tmp}" "${private_key}"
    mv "${public_tmp}" "${public_key}"
  fi

  local derived_public
  derived_public="$(mktemp "${SECRETS_DIR}/.${temporary_label}-derived-public.XXXXXX")"
  if ! openssl pkey -in "${private_key}" -pubout -out "${derived_public}" \
      >/dev/null 2>&1 || \
    ! openssl pkey -pubin -in "${public_key}" -text -noout 2>/dev/null | \
      grep -Fq "ED25519" || \
    ! cmp -s "${derived_public}" "${public_key}"; then
    rm -f -- "${derived_public}"
    echo "${key_label} key pair is invalid or mismatched" >&2
    exit 2
  fi
  rm -f -- "${derived_public}"
  chmod 400 "${private_key}"
  chmod 444 "${public_key}"
}

ensure_ed25519_signing_keys \
  backup_manifest "backup manifest signing" backup-manifest
ensure_ed25519_signing_keys \
  restore_attestation "restore attestation signing" restore-attestation
if cmp -s \
  "${SECRETS_DIR}/backup_manifest_signing_public_key.pem" \
  "${SECRETS_DIR}/restore_attestation_signing_public_key.pem"; then
  echo "backup manifest and restore attestation must use distinct Ed25519 keys" >&2
  exit 2
fi

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
audio_inference_token="$(secret_value audio_inference_api_token "$(random_hex 32)")"
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
write_once platform_credential_bindings "{}"

echo "Secrets initialized in ${SECRETS_DIR}. Back them up in an external secret manager."
echo "TLS certificate/key are intentionally not generated; install them under production/tls."
