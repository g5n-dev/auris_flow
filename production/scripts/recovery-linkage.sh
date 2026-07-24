#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRODUCTION_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPOSITORY_ROOT="$(cd "${PRODUCTION_ROOT}/.." && pwd)"
COMPOSE_FILE="${PRODUCTION_ROOT}/compose.yaml"
LINKAGE_TOOL="${PRODUCTION_ROOT}/backup/recovery_linkage.py"
DEADLINE_RUNNER="${REPOSITORY_ROOT}/scripts/run_with_deadline.py"
PYTHON="${PYTHON:-python3}"
DOCKER_CONTEXT_NAME="default"
IO_TIMEOUT_SECONDS="${AURIS_RECOVERY_LINKAGE_IO_TIMEOUT_SECONDS:-120}"
ACTION="${1:-}"
PROJECT_NAME=""
ENV_FILE="${AURIS_COMPOSE_ENV_FILE:-${PRODUCTION_ROOT}/.env}"
PROOF_OUTPUT=""
PRIVATE_ROOT=""
PRIVATE_PARENT=""
MINIO_OBJECT="auris/auris-flow/release-gate/recovery-linkage-v1.json"

usage() {
  cat <<'USAGE'
Usage: production/scripts/recovery-linkage.sh ACTION --project-name NAME \
       --env-file ABSOLUTE_FILE --proof-output ABSOLUTE_FILE

ACTION is exactly one of:
  seed      Create the synthetic authority fixture in an empty source stack.
  capture   Independently read MySQL, MinIO, and Qdrant and publish a proof.
  rebuild   Recreate the synthetic Qdrant point from restored MySQL and MinIO.

The output contains only fixed synthetic identifiers and SHA-256 digests.
Raw authority JSON, object bytes, Qdrant payloads, and credentials stay in an
owner-only temporary directory and are removed on every exit path.
USAGE
}

fail() {
  printf 'recovery linkage failed: %s\n' "$1" >&2
  exit 2
}

has_control_character() {
  [[ "$1" == *$'\n'* || "$1" == *$'\r'* || "$1" == *$'\t'* ]]
}

[[ -n "${ACTION}" ]] || {
  usage >&2
  exit 2
}
shift

while (($#)); do
  case "$1" in
    --project-name)
      (($# >= 2)) || fail "--project-name requires a value"
      PROJECT_NAME="$2"
      shift 2
      ;;
    --env-file)
      (($# >= 2)) || fail "--env-file requires a value"
      ENV_FILE="$2"
      shift 2
      ;;
    --proof-output)
      (($# >= 2)) || fail "--proof-output requires a value"
      PROOF_OUTPUT="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "unknown option"
      ;;
  esac
done

[[ "${ACTION}" == "seed" || "${ACTION}" == "capture" || \
  "${ACTION}" == "rebuild" ]] || fail "action must be seed, capture, or rebuild"
[[ "${IO_TIMEOUT_SECONDS}" =~ ^[1-9][0-9]{0,2}$ ]] && \
  ((10#${IO_TIMEOUT_SECONDS} <= 300)) || fail \
  "recovery linkage I/O timeout must be an integer from 1 to 300 seconds"
[[ "${PROJECT_NAME}" =~ ^[a-z0-9][a-z0-9_-]{0,62}$ ]] || fail \
  "Compose project name is invalid"
for path_value in "${ENV_FILE}" "${PROOF_OUTPUT}"; do
  has_control_character "${path_value}" && fail "input path contains a control character"
  [[ "${path_value}" == /* ]] || fail "file paths must be absolute"
done
[[ -f "${ENV_FILE}" && ! -L "${ENV_FILE}" ]] || fail \
  "Compose environment file is missing or unsafe"
[[ ! -e "${PROOF_OUTPUT}" && ! -L "${PROOF_OUTPUT}" ]] || fail \
  "proof output already exists or is unsafe"
[[ -d "$(dirname "${PROOF_OUTPUT}")" && \
  ! -L "$(dirname "${PROOF_OUTPUT}")" ]] || fail \
  "proof output parent is missing or unsafe"
[[ -f "${COMPOSE_FILE}" && ! -L "${COMPOSE_FILE}" ]] || fail \
  "Compose file is missing or unsafe"
[[ -f "${LINKAGE_TOOL}" && ! -L "${LINKAGE_TOOL}" ]] || fail \
  "recovery linkage tool is missing or unsafe"
[[ -f "${DEADLINE_RUNNER}" && ! -L "${DEADLINE_RUNNER}" ]] || fail \
  "deadline runner is missing or unsafe"
PRIVATE_PARENT="${TMPDIR:-/tmp}"
has_control_character "${PRIVATE_PARENT}" && fail \
  "temporary directory contains a control character"
[[ "${PRIVATE_PARENT}" == /* && -d "${PRIVATE_PARENT}" ]] || fail \
  "temporary directory must be an absolute existing directory"
PRIVATE_PARENT="$(cd -P -- "${PRIVATE_PARENT}" && pwd -P)" || fail \
  "temporary directory could not be resolved"
[[ -z "${DOCKER_HOST:-}" && -z "${DOCKER_CONTEXT:-}" && \
  -z "${COMPOSE_PROJECT_NAME:-}" ]] || fail \
  "DOCKER_HOST, DOCKER_CONTEXT and COMPOSE_PROJECT_NAME overrides are forbidden"
for command_name in docker mktemp find id "${PYTHON}"; do
  command -v "${command_name}" >/dev/null 2>&1 || fail \
    "required command is unavailable"
done

cleanup_private_root() {
  local failed=0
  [[ -n "${PRIVATE_ROOT}" ]] || return 0
  case "${PRIVATE_ROOT}" in
    "${PRIVATE_PARENT}"/auris-flow-recovery-linkage.*)
      if [[ -d "${PRIVATE_ROOT}" && ! -L "${PRIVATE_ROOT}" ]]; then
        find "${PRIVATE_ROOT}" -mindepth 1 -maxdepth 1 -type f -delete || failed=1
        rmdir -- "${PRIVATE_ROOT}" || failed=1
      else
        failed=1
      fi
      ;;
    *)
      printf 'recovery linkage private path failed its cleanup boundary\n' >&2
      failed=1
      ;;
  esac
  if [[ "${failed}" -eq 0 ]]; then
    PRIVATE_ROOT=""
  fi
  return "${failed}"
}

cleanup() {
  local status="$?"
  trap - EXIT INT TERM
  cleanup_private_root || status=1
  exit "${status}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

PRIVATE_ROOT="$(mktemp -d "${PRIVATE_PARENT}/auris-flow-recovery-linkage.XXXXXX")"
chmod 0700 "${PRIVATE_ROOT}"
CANDIDATE_PROOF="${PRIVATE_ROOT}/candidate-proof.json"

compose_with_deadline() {
  local label="$1"
  shift
  COMPOSE_PROJECT_NAME="${PROJECT_NAME}" \
    "${PYTHON}" "${DEADLINE_RUNNER}" \
    --timeout-seconds "${IO_TIMEOUT_SECONDS}" \
    --label "${label}" -- \
    docker --context "${DOCKER_CONTEXT_NAME}" compose \
    --project-name "${PROJECT_NAME}" \
    --project-directory "${PRODUCTION_ROOT}" \
    --env-file "${ENV_FILE}" \
    -f "${COMPOSE_FILE}" "$@"
}

minio_mc_with_deadline() {
  local label="$1"
  shift
  compose_with_deadline "${label}" run --rm --no-deps -T \
    --entrypoint /opt/auris/minio-client.sh \
    minio-bootstrap "$@"
}

capture_authority() {
  local label="$1" output="$2"
  local query="${PRIVATE_ROOT}/${label}-mysql-export.sql"
  "${PYTHON}" "${LINKAGE_TOOL}" write-mysql-export-sql --output "${query}"
  compose_with_deadline "MySQL recovery linkage export" exec -T mysql sh -c '
    export MYSQL_PWD="$(cat /run/secrets/mysql_root_password)"
    exec mysql --protocol=tcp --host=127.0.0.1 --user=root \
      --database=auris_flow --batch --raw --skip-column-names
  ' <"${query}" | "${PYTHON}" "${LINKAGE_TOOL}" capture-json-stdin \
    --output "${output}"
}

capture_object() {
  local output="$1"
  minio_mc_with_deadline "MinIO recovery linkage stat" \
    stat --json "${MINIO_OBJECT}" | \
    "${PYTHON}" "${LINKAGE_TOOL}" validate-object-stat-stdin
  minio_mc_with_deadline "MinIO recovery linkage object read" \
    cat "${MINIO_OBJECT}" | \
    "${PYTHON}" "${LINKAGE_TOOL}" capture-object-stdin --output "${output}"
}

qdrant_export() {
  local output_name="$1"
  compose_with_deadline "Qdrant recovery linkage export" run --rm --no-deps -T \
    --user "$(id -u):$(id -g)" \
    --volume "${PRIVATE_ROOT}:/work:rw" \
    --volume "${LINKAGE_TOOL}:/opt/auris/recovery-linkage.py:ro" \
    --entrypoint python qdrant-backup-tool \
    /opt/auris/recovery-linkage.py qdrant-export \
    --point-output "/work/${output_name}"
}

qdrant_seed() {
  local authority_name="$1" object_name="$2" output_name="$3"
  compose_with_deadline "Qdrant recovery linkage seed" run --rm --no-deps -T \
    --user "$(id -u):$(id -g)" \
    --volume "${PRIVATE_ROOT}:/work:rw" \
    --volume "${LINKAGE_TOOL}:/opt/auris/recovery-linkage.py:ro" \
    --entrypoint python qdrant-backup-tool \
    /opt/auris/recovery-linkage.py qdrant-seed \
    --authority-json "/work/${authority_name}" \
    --object-file "/work/${object_name}" \
    --point-output "/work/${output_name}"
}

capture_proof() {
  local label="$1" proof_output="$2"
  local authority="${PRIVATE_ROOT}/${label}-authority.json"
  local object="${PRIVATE_ROOT}/${label}-object.json"
  local point_name="${label}-qdrant-point.json"
  capture_authority "${label}" "${authority}"
  capture_object "${object}"
  qdrant_export "${point_name}"
  "${PYTHON}" "${LINKAGE_TOOL}" build-proof \
    --authority-json "${authority}" \
    --object-file "${object}" \
    --qdrant-point-json "${PRIVATE_ROOT}/${point_name}" \
    --output "${proof_output}"
}

case "${ACTION}" in
  seed)
    "${PYTHON}" "${LINKAGE_TOOL}" write-fixture \
      --authority-output "${PRIVATE_ROOT}/seed-authority.json" \
      --object-output "${PRIVATE_ROOT}/seed-object.json"
    "${PYTHON}" "${LINKAGE_TOOL}" write-mysql-seed-sql \
      --output "${PRIVATE_ROOT}/seed-mysql.sql"
    compose_with_deadline "MinIO recovery linkage seed" run --rm --no-deps -T \
      --volume "${PRIVATE_ROOT}:/work:ro" \
      --entrypoint /opt/auris/minio-client.sh \
      minio-bootstrap cp \
      /work/seed-object.json "${MINIO_OBJECT}"
    compose_with_deadline "MySQL recovery linkage seed" exec -T mysql sh -c '
      export MYSQL_PWD="$(cat /run/secrets/mysql_root_password)"
      exec mysql --protocol=tcp --host=127.0.0.1 --user=root \
        --database=auris_flow --binary-mode=1
    ' <"${PRIVATE_ROOT}/seed-mysql.sql"
    capture_authority "seed-live" "${PRIVATE_ROOT}/seed-live-authority.json"
    capture_object "${PRIVATE_ROOT}/seed-live-object.json"
    qdrant_seed \
      "seed-live-authority.json" "seed-live-object.json" "seed-live-qdrant-point.json"
    capture_proof "seed-verified" "${CANDIDATE_PROOF}"
    ;;
  capture)
    capture_proof "capture" "${CANDIDATE_PROOF}"
    ;;
  rebuild)
    capture_authority "rebuild-live" "${PRIVATE_ROOT}/rebuild-live-authority.json"
    capture_object "${PRIVATE_ROOT}/rebuild-live-object.json"
    qdrant_seed \
      "rebuild-live-authority.json" \
      "rebuild-live-object.json" \
      "rebuild-live-qdrant-point.json"
    capture_proof "rebuild-verified" "${CANDIDATE_PROOF}"
    ;;
esac

"${PYTHON}" "${LINKAGE_TOOL}" validate-proof --input "${CANDIDATE_PROOF}"
proof_document="$(<"${CANDIDATE_PROOF}")"
cleanup_private_root || fail "private recovery material cleanup failed"
printf '%s' "${proof_document}" | \
  "${PYTHON}" "${LINKAGE_TOOL}" publish-proof-stdin --output "${PROOF_OUTPUT}"
printf 'Cross-store recovery linkage %s completed with a digest-only proof.\n' \
  "${ACTION}"
