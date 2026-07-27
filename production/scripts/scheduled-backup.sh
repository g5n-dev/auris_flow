#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRODUCTION_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
COMPOSE_FILE="${PRODUCTION_ROOT}/compose.yaml"
BACKUP_SCRIPT="${SCRIPT_DIR}/backup.sh"
ENV_FILE="${AURIS_COMPOSE_ENV_FILE:-${PRODUCTION_ROOT}/.env}"
OUTPUT_ROOT="${AURIS_BACKUP_OUTPUT_ROOT:-}"
LOCK_FILE="${AURIS_BACKUP_LOCK_FILE:-/run/lock/auris-flow-backup.lock}"
STOP_TIMEOUT="${AURIS_BACKUP_STOP_TIMEOUT_SECONDS:-120}"
RESTART_WAIT_TIMEOUT="${AURIS_BACKUP_RESTART_WAIT_SECONDS:-240}"
PRODUCTION_PROJECT_NAME="auris-flow"
DOCKER_CONTEXT_NAME="default"

WRITER_SERVICES=(
  edge
  worker
  bff
  dagster-daemon
  dagster-webserver
  dagster-code
  keycloak
)
ONE_SHOT_WRITERS=(
  migrate
  dagster-storage-bootstrap
  db-bootstrap
  minio-volume-init
  minio-bootstrap
  identity-bootstrap
)
RUNNING_SERVICES=()
ORIGINAL_WRITERS=()
MAINTENANCE_ENTERED=false

fail() {
  printf 'scheduled backup failed: %s\n' "$1" >&2
  exit 2
}

has_control_character() {
  [[ "$1" == *$'\n'* || "$1" == *$'\r'* || "$1" == *$'\t'* ]]
}

compose() {
  COMPOSE_PROJECT_NAME="${PRODUCTION_PROJECT_NAME}" \
    docker --context "${DOCKER_CONTEXT_NAME}" compose \
    --project-name "${PRODUCTION_PROJECT_NAME}" \
    --project-directory "${PRODUCTION_ROOT}" \
    --env-file "${ENV_FILE}" \
    --file "${COMPOSE_FILE}" "$@"
}

was_running() {
  local target="$1" service
  for service in "${RUNNING_SERVICES[@]}"; do
    [[ "${service}" == "${target}" ]] && return 0
  done
  return 1
}

was_original_writer() {
  local target="$1" service
  for service in "${ORIGINAL_WRITERS[@]}"; do
    [[ "${service}" == "${target}" ]] && return 0
  done
  return 1
}

restart_phase() {
  local candidate
  local services_to_start=()
  for candidate in "$@"; do
    if was_original_writer "${candidate}"; then
      services_to_start+=("${candidate}")
    fi
  done
  ((${#services_to_start[@]} == 0)) && return 0
  compose up -d --no-deps --wait \
    --wait-timeout "${RESTART_WAIT_TIMEOUT}" \
    "${services_to_start[@]}"
}

restart_writers() {
  [[ "${MAINTENANCE_ENTERED}" == true ]] || return 0
  printf 'Restoring the pre-backup writer service set...\n'
  restart_phase keycloak || return 1
  restart_phase dagster-code || return 1
  restart_phase dagster-webserver dagster-daemon || return 1
  restart_phase bff || return 1
  restart_phase worker || return 1
  restart_phase edge || return 1
  MAINTENANCE_ENTERED=false
}

restore_on_exit() {
  local status=$?
  trap - EXIT INT TERM
  if ! restart_writers; then
    printf 'scheduled backup failed: writer services did not fully recover\n' >&2
    status=3
  fi
  exit "${status}"
}

for command_name in docker flock; do
  command -v "${command_name}" >/dev/null 2>&1 || fail \
    "required command not found: ${command_name}"
done
[[ -x "${BACKUP_SCRIPT}" && ! -L "${BACKUP_SCRIPT}" ]] || fail \
  "governed backup script is missing or unsafe"
[[ -f "${COMPOSE_FILE}" && ! -L "${COMPOSE_FILE}" ]] || fail \
  "Compose file is missing or unsafe"
[[ -f "${ENV_FILE}" && ! -L "${ENV_FILE}" ]] || fail \
  "Compose env file is missing or unsafe"
[[ "${OUTPUT_ROOT}" == /* ]] || fail \
  "AURIS_BACKUP_OUTPUT_ROOT must be an absolute encrypted-external path"
[[ "${LOCK_FILE}" == /* ]] || fail "backup lock file must be absolute"
has_control_character "${OUTPUT_ROOT}" && fail "backup output path contains a control character"
has_control_character "${LOCK_FILE}" && fail "backup lock path contains a control character"
[[ "${STOP_TIMEOUT}" =~ ^[0-9]+$ && "${RESTART_WAIT_TIMEOUT}" =~ ^[0-9]+$ ]] || fail \
  "backup stop/restart timeouts must be positive integers"
((STOP_TIMEOUT >= 30 && STOP_TIMEOUT <= 600)) || fail \
  "backup stop timeout must be between 30 and 600 seconds"
((RESTART_WAIT_TIMEOUT >= 60 && RESTART_WAIT_TIMEOUT <= 900)) || fail \
  "backup restart wait timeout must be between 60 and 900 seconds"
[[ -d "$(dirname "${LOCK_FILE}")" && ! -L "$(dirname "${LOCK_FILE}")" ]] || fail \
  "backup lock directory is missing or unsafe"

exec 9>"${LOCK_FILE}"
flock -n 9 || fail "another backup or maintenance window is already active"

compose config --quiet || fail "Compose configuration is invalid"
mapfile -t RUNNING_SERVICES < <(compose ps --status running --services)
for service in "${ONE_SHOT_WRITERS[@]}"; do
  was_running "${service}" && fail \
    "one-shot writer ${service} is running; refusing to enter maintenance"
done
for service in "${WRITER_SERVICES[@]}"; do
  if was_running "${service}"; then
    ORIGINAL_WRITERS+=("${service}")
  fi
done

MAINTENANCE_ENTERED=true
trap restore_on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if was_original_writer edge; then
  compose stop --timeout "${STOP_TIMEOUT}" edge
fi
remaining_writers=()
for service in worker bff dagster-daemon dagster-webserver dagster-code keycloak; do
  if was_original_writer "${service}"; then
    remaining_writers+=("${service}")
  fi
done
if ((${#remaining_writers[@]})); then
  compose stop --timeout "${STOP_TIMEOUT}" "${remaining_writers[@]}"
fi

RUNNING_SERVICES=()
mapfile -t RUNNING_SERVICES < <(compose ps --status running --services)
for service in "${WRITER_SERVICES[@]}" "${ONE_SHOT_WRITERS[@]}"; do
  was_running "${service}" && fail \
    "writer ${service} remains active after maintenance stop"
done

"${BACKUP_SCRIPT}" \
  --output-root "${OUTPUT_ROOT}" \
  --storage-boundary encrypted-external
