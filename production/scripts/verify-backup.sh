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
BACKUP_EVIDENCE_TOOL="${BACKUP_TOOLS}/backup_restore_evidence.py"
BACKUP_EVIDENCE_VALIDATOR="${REPOSITORY_ROOT}/scripts/verify_backup_restore_gate.py"
RESTORE_NETWORK_ALLOCATOR="${BACKUP_TOOLS}/restore_network_allocator.py"
RECOVERY_LINKAGE_TOOL="${BACKUP_TOOLS}/recovery_linkage.py"
RECOVERY_LINKAGE_SCRIPT="${SCRIPT_DIR}/recovery-linkage.sh"
PYTHON="${PYTHON:-python3}"
ENV_FILE="${AURIS_COMPOSE_ENV_FILE:-${PRODUCTION_ROOT}/.env}"
SECRETS_DIR="${AURIS_SECRETS_DIR:-${PRODUCTION_ROOT}/secrets}"
MANIFEST_VERIFY_KEY_FILE="${AURIS_BACKUP_MANIFEST_VERIFY_KEY_FILE:-${SECRETS_DIR}/backup_manifest_signing_public_key.pem}"
BACKUP_ROOT=""
SOURCE_BACKUP_ROOT=""
VERIFY_SNAPSHOT_ROOT=""
RUN_DRILL=false
CLEANUP_ON_SUCCESS=false
DRILL_PROJECT=""
ALLOW_RELEASE_MIGRATION_FROM=""
VALIDATION_SCRIPT=""
EVIDENCE_OUTPUT=""
EVIDENCE_PREPARED_JSON=""
VERIFIED_MANIFEST_FILE=""
DOCKER_CONTEXT_EVIDENCE_FILE=""
DOCKER_INFO_EVIDENCE_FILE=""
BACKUP_VERIFICATION_STARTED_AT=""
BACKUP_VERIFICATION_COMPLETED_AT=""
RESTORE_STARTED_AT=""
RESTORE_COMPLETED_AT=""
DRILL_INTERNAL_SUBNET=""
DRILL_EDGE_INTERNAL_IP=""
LINKAGE_PRIVATE_ROOT=""
SOURCE_LINKAGE_PROOF=""
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
  --evidence-output ABSOLUTE_FILE
                          Atomically publish formal release evidence; requires
                          --drill and --cleanup-on-success on native Linux
  --env-file FILE         Compose environment file required by --drill
  --manifest-public-key FILE
                          Deployment-owned Ed25519 trust-anchor public key
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
  local cleanup_started_at="" cleanup_completed_at="" verified_at=""
  local evidence_drill_project="" remaining_containers="" remaining_volumes=""
  local remaining_networks=""
  trap - EXIT INT TERM
  set +e
  if [[ -n "${VALIDATION_SCRIPT}" && -e "${VALIDATION_SCRIPT}" ]]; then
    if ! rm -f -- "${VALIDATION_SCRIPT}"; then
      printf 'backup verification cleanup failed for validation script: %s\n' \
        "${VALIDATION_SCRIPT}" >&2
      cleanup_failed=1
    fi
  fi
  for private_file in \
    "${VERIFIED_MANIFEST_FILE}" \
    "${DOCKER_CONTEXT_EVIDENCE_FILE}" \
    "${DOCKER_INFO_EVIDENCE_FILE}"; do
    if [[ -n "${private_file}" && -e "${private_file}" ]] && \
      ! rm -f -- "${private_file}"; then
      printf 'backup verification cleanup failed for a private evidence input\n' >&2
      cleanup_failed=1
    fi
  done
  if [[ -n "${VERIFY_SNAPSHOT_ROOT}" && -d "${VERIFY_SNAPSHOT_ROOT}" ]]; then
    if "${PYTHON}" "${BACKUP_TOOLS}/manifest.py" destroy-snapshot \
      --snapshot-root "${VERIFY_SNAPSHOT_ROOT}" >/dev/null 2>&1; then
      VERIFY_SNAPSHOT_ROOT=""
    elif rmdir -- "${VERIFY_SNAPSHOT_ROOT}" >/dev/null 2>&1; then
      VERIFY_SNAPSHOT_ROOT=""
    else
      printf 'backup verification cleanup failed for private snapshot: %s\n' \
        "${VERIFY_SNAPSHOT_ROOT}" >&2
      cleanup_failed=1
    fi
  fi
  if [[ -n "${LINKAGE_PRIVATE_ROOT}" ]]; then
    case "${LINKAGE_PRIVATE_ROOT}" in
      "${TMPDIR:-/tmp}"/auris-flow-linkage-verification.*)
        if [[ -d "${LINKAGE_PRIVATE_ROOT}" && \
          ! -L "${LINKAGE_PRIVATE_ROOT}" ]]; then
          if ! rm -f -- "${LINKAGE_PRIVATE_ROOT}/restored-proof.json" || \
            ! rmdir -- "${LINKAGE_PRIVATE_ROOT}"; then
            printf 'backup verification cleanup failed for cross-store proof inputs\n' >&2
            cleanup_failed=1
          fi
        fi
        ;;
      *)
        printf 'cross-store proof cleanup path failed its boundary\n' >&2
        cleanup_failed=1
        ;;
    esac
    LINKAGE_PRIVATE_ROOT=""
  fi
  if [[ "${status}" -eq 0 && "${CLEANUP_ON_SUCCESS}" == true && \
    -n "${DRILL_PROJECT}" ]]; then
    cleanup_started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    if compose_drill_with_deadline "${DRILL_CLEANUP_TIMEOUT}" \
      "clean restore drill project" \
      down --volumes --remove-orphans --timeout 20 >/dev/null 2>&1; then
      if ! remaining_containers="$(
        docker --context "${DOCKER_CONTEXT_NAME}" ps --all --quiet \
          --filter "label=com.docker.compose.project=${DRILL_PROJECT}"
      )"; then
        printf 'could not verify exact-project container cleanup\n' >&2
        cleanup_failed=1
      fi
      if ! remaining_volumes="$(
        docker --context "${DOCKER_CONTEXT_NAME}" volume ls --quiet --filter \
          "label=com.docker.compose.project=${DRILL_PROJECT}"
      )"; then
        printf 'could not verify exact-project volume cleanup\n' >&2
        cleanup_failed=1
      fi
      if ! remaining_networks="$(
        docker --context "${DOCKER_CONTEXT_NAME}" network ls --quiet --filter \
          "label=com.docker.compose.project=${DRILL_PROJECT}"
      )"; then
        printf 'could not verify exact-project network cleanup\n' >&2
        cleanup_failed=1
      fi
      if [[ -n "${remaining_containers}" || -n "${remaining_volumes}" || \
        -n "${remaining_networks}" ]]; then
        printf 'drill cleanup left project-labelled runtime objects: %s\n' \
          "${DRILL_PROJECT}" >&2
        cleanup_failed=1
      fi
      if [[ "${cleanup_failed}" -eq 0 ]]; then
        cleanup_completed_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        printf 'Removed isolated project and volumes: %s\n' "${DRILL_PROJECT}"
        evidence_drill_project="${DRILL_PROJECT}"
        DRILL_PROJECT=""
      fi
    else
      printf 'drill passed but exact-project cleanup failed: %s\n' \
        "${DRILL_PROJECT}" >&2
      cleanup_failed=1
    fi
  fi
  if [[ "${status}" -eq 0 && "${cleanup_failed}" -eq 0 && \
    -n "${EVIDENCE_OUTPUT}" ]]; then
    if [[ -z "${EVIDENCE_PREPARED_JSON}" || -z "${evidence_drill_project}" || \
      -z "${cleanup_started_at}" || -z "${cleanup_completed_at}" ]]; then
      printf 'formal evidence inputs were not completed\n' >&2
      cleanup_failed=1
    else
      verified_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
      if ! printf '%s\n' "${EVIDENCE_PREPARED_JSON}" | \
        "${PYTHON}" "${BACKUP_EVIDENCE_TOOL}" emit-gate \
          --root "${REPOSITORY_ROOT}" \
          --drill-project "${evidence_drill_project}" \
          --restore-subnet "${DRILL_INTERNAL_SUBNET}" \
          --edge-internal-ip "${DRILL_EDGE_INTERNAL_IP}" \
          --backup-verification-started-at "${BACKUP_VERIFICATION_STARTED_AT}" \
          --backup-verification-completed-at "${BACKUP_VERIFICATION_COMPLETED_AT}" \
          --restore-started-at "${RESTORE_STARTED_AT}" \
          --restore-completed-at "${RESTORE_COMPLETED_AT}" \
          --cleanup-started-at "${cleanup_started_at}" \
          --cleanup-completed-at "${cleanup_completed_at}" \
          --verified-at "${verified_at}" \
          --output "${EVIDENCE_OUTPUT}"; then
        printf 'formal backup/restore evidence was not published\n' >&2
        cleanup_failed=1
      fi
    fi
  fi
  EVIDENCE_PREPARED_JSON=""
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
    --evidence-output)
      (($# >= 2)) || fail "--evidence-output requires a value"
      EVIDENCE_OUTPUT="$2"
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

if [[ -n "${EVIDENCE_OUTPUT}" && \
  ( "${RUN_DRILL}" != true || "${CLEANUP_ON_SUCCESS}" != true ) ]]; then
  fail "--evidence-output requires --drill and --cleanup-on-success"
fi
if [[ -n "${EVIDENCE_OUTPUT}" ]]; then
  has_control_character "${EVIDENCE_OUTPUT}" && fail \
    "evidence output path contains a control character"
  [[ "${EVIDENCE_OUTPUT}" == /* ]] || fail \
    "--evidence-output must be an absolute path"
  [[ -z "${ALLOW_RELEASE_MIGRATION_FROM}" ]] || fail \
    "formal evidence does not accept predecessor-migration drills"
  [[ -d "$(dirname "${EVIDENCE_OUTPUT}")" && \
    ! -L "$(dirname "${EVIDENCE_OUTPUT}")" ]] || fail \
    "evidence output parent must be a real directory"
  evidence_output_parent="$(
    cd "$(dirname "${EVIDENCE_OUTPUT}")" && pwd -P
  )"
  EVIDENCE_OUTPUT="${evidence_output_parent}/$(basename "${EVIDENCE_OUTPUT}")"
  [[ ! -e "${EVIDENCE_OUTPUT}" && ! -L "${EVIDENCE_OUTPUT}" ]] || fail \
    "evidence output already exists"
  [[ -f "${BACKUP_EVIDENCE_TOOL}" && ! -L "${BACKUP_EVIDENCE_TOOL}" ]] || fail \
    "backup/restore evidence producer is missing or unsafe"
  [[ -f "${BACKUP_EVIDENCE_VALIDATOR}" && \
    ! -L "${BACKUP_EVIDENCE_VALIDATOR}" ]] || fail \
    "backup/restore evidence validator is missing or unsafe"
fi

command -v "${PYTHON}" >/dev/null 2>&1 || fail "Python is required"
command -v openssl >/dev/null 2>&1 || fail "OpenSSL is required"
command -v cmp >/dev/null 2>&1 || fail "cmp is required"
[[ -f "${RECOVERY_LINKAGE_TOOL}" && ! -L "${RECOVERY_LINKAGE_TOOL}" ]] || fail \
  "cross-store recovery linkage tool is missing or unsafe"
[[ -f "${RECOVERY_LINKAGE_SCRIPT}" && ! -L "${RECOVERY_LINKAGE_SCRIPT}" ]] || fail \
  "cross-store recovery linkage script is missing or unsafe"
has_control_character "${BACKUP_ROOT}" && fail "backup path contains a control character"
has_control_character "${ENV_FILE}" && fail "Compose env path contains a control character"
has_control_character "${MANIFEST_VERIFY_KEY_FILE}" && fail \
  "manifest public-key path contains a control character"
if [[ -n "${ALLOW_RELEASE_MIGRATION_FROM}" && \
  ! "${ALLOW_RELEASE_MIGRATION_FROM}" =~ ^[0-9a-f]{40}$ && \
  ! "${ALLOW_RELEASE_MIGRATION_FROM}" =~ ^[0-9a-f]{64}$ ]]; then
  fail "--allow-release-migration-from must be a complete lowercase Git id"
fi
[[ "${BACKUP_ROOT}" == /* ]] || fail "--backup must be an absolute path"
[[ "${MANIFEST_VERIFY_KEY_FILE}" == /* ]] || fail \
  "backup manifest public-key path must be absolute"
[[ -f "${MANIFEST_VERIFY_KEY_FILE}" && ! -L "${MANIFEST_VERIFY_KEY_FILE}" ]] || fail \
  "backup manifest public trust-key file is missing or unsafe"
MANIFEST_VERIFY_KEY_FILE="$(cd "$(dirname "${MANIFEST_VERIFY_KEY_FILE}")" && pwd -P)/$(basename "${MANIFEST_VERIFY_KEY_FILE}")"
"${PYTHON}" "${BACKUP_TOOLS}/manifest.py" key-id \
  --public-key "${MANIFEST_VERIFY_KEY_FILE}" >/dev/null || fail \
  "backup manifest Ed25519 trust anchor is invalid"
[[ -d "${BACKUP_ROOT}" && ! -L "${BACKUP_ROOT}" ]] || fail \
  "backup root must be a real directory, not a symlink"
BACKUP_ROOT="$(cd "${BACKUP_ROOT}" && pwd -P)"
[[ "${BACKUP_ROOT}" != "/" ]] || fail "backup root is too broad"
paths_overlap "${MANIFEST_VERIFY_KEY_FILE}" "${BACKUP_ROOT}" && fail \
  "backup manifest public trust key must be external to the backup"
paths_overlap "${BACKUP_ROOT}" "${REPOSITORY_ROOT}" && fail \
  "backup path must not be an ancestor or descendant of the release bundle"

SOURCE_BACKUP_ROOT="${BACKUP_ROOT}"
if [[ -n "${EVIDENCE_OUTPUT}" ]]; then
  paths_overlap "${EVIDENCE_OUTPUT}" "${SOURCE_BACKUP_ROOT}" && fail \
    "evidence output must be external to the signed backup"
  paths_overlap "${EVIDENCE_OUTPUT}" "${REPOSITORY_ROOT}" && fail \
    "evidence output must be external to the signed deployment bundle"
  paths_overlap "${EVIDENCE_OUTPUT}" "${MANIFEST_VERIFY_KEY_FILE}" && fail \
    "evidence output must not overlap the manifest trust anchor"
  BACKUP_VERIFICATION_STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
fi
VERIFY_SNAPSHOT_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/auris-flow-verify-snapshot.XXXXXX")"
"${PYTHON}" "${BACKUP_TOOLS}/manifest.py" snapshot \
  --source "${SOURCE_BACKUP_ROOT}" \
  --snapshot-root "${VERIFY_SNAPSHOT_ROOT}" \
  --public-key "${MANIFEST_VERIFY_KEY_FILE}" >/dev/null || fail \
  "could not create a private signed-backup verification snapshot"
BACKUP_ROOT="${VERIFY_SNAPSHOT_ROOT}/backup"

verified_manifest_json="$("${PYTHON}" "${BACKUP_TOOLS}/manifest.py" verify \
  --root "${BACKUP_ROOT}" \
  --public-key "${MANIFEST_VERIFY_KEY_FILE}")" || fail \
  "external manifest signature or artifact verification failed"
if [[ -n "${EVIDENCE_OUTPUT}" ]]; then
  VERIFIED_MANIFEST_FILE="$(
    mktemp "${TMPDIR:-/tmp}/auris-verified-manifest.XXXXXX"
  )"
  printf '%s\n' "${verified_manifest_json}" >"${VERIFIED_MANIFEST_FILE}"
fi
printf '%s\n' "${verified_manifest_json}"
"${PYTHON}" "${BACKUP_TOOLS}/mysql_dump.py" verify \
  --input "${BACKUP_ROOT}/mysql/all-databases.sql.gz"
"${PYTHON}" "${BACKUP_TOOLS}/minio_versions.py" verify-artifacts \
  --plan "${BACKUP_ROOT}/minio/versions.json" \
  --backup-root "${BACKUP_ROOT}" >/dev/null
VALIDATION_SCRIPT="$(mktemp "${TMPDIR:-/tmp}/auris-minio-validate.XXXXXX")"
"${PYTHON}" "${BACKUP_TOOLS}/minio_versions.py" emit-restore-shell \
  --plan "${BACKUP_ROOT}/minio/versions.json" \
  --output "${VALIDATION_SCRIPT}"
bash -n "${VALIDATION_SCRIPT}"
rm -f -- "${VALIDATION_SCRIPT}"
VALIDATION_SCRIPT=""
"${PYTHON}" "${BACKUP_TOOLS}/qdrant_snapshots.py" validate \
  --input "${BACKUP_ROOT}/qdrant"
SOURCE_LINKAGE_PROOF="${BACKUP_ROOT}/metadata/recovery-linkage.json"
if [[ -f "${SOURCE_LINKAGE_PROOF}" && ! -L "${SOURCE_LINKAGE_PROOF}" ]]; then
  "${PYTHON}" "${RECOVERY_LINKAGE_TOOL}" validate-proof \
    --input "${SOURCE_LINKAGE_PROOF}" || fail \
    "signed cross-store source proof is invalid"
elif [[ -n "${EVIDENCE_OUTPUT}" ]]; then
  fail "formal release evidence requires a signed cross-store source proof"
else
  SOURCE_LINKAGE_PROOF=""
fi
if [[ -n "${EVIDENCE_OUTPUT}" ]]; then
  BACKUP_VERIFICATION_COMPLETED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
fi
printf 'Offline backup verification passed.\n'

if [[ "${RUN_DRILL}" != true ]]; then
  exit 0
fi

command -v docker >/dev/null 2>&1 || fail "Docker is required for --drill"
command -v cosign >/dev/null 2>&1 || fail "Cosign is required for --drill"
command -v ip >/dev/null 2>&1 || fail \
  "iproute2 is required for isolated --drill networking"
[[ -f "${RESTORE_NETWORK_ALLOCATOR}" && \
  ! -L "${RESTORE_NETWORK_ALLOCATOR}" ]] || fail \
  "restore network allocator is missing or unsafe"
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
if [[ -n "${EVIDENCE_OUTPUT}" ]]; then
  DOCKER_CONTEXT_EVIDENCE_FILE="$(
    mktemp "${TMPDIR:-/tmp}/auris-docker-context.XXXXXX"
  )"
  DOCKER_INFO_EVIDENCE_FILE="$(
    mktemp "${TMPDIR:-/tmp}/auris-docker-info.XXXXXX"
  )"
  docker --context "${DOCKER_CONTEXT_NAME}" context inspect \
    "${DOCKER_CONTEXT_NAME}" >"${DOCKER_CONTEXT_EVIDENCE_FILE}" || fail \
    "could not inspect the formal-evidence Docker context"
  docker --context "${DOCKER_CONTEXT_NAME}" info --format '{{json .}}' \
    >"${DOCKER_INFO_EVIDENCE_FILE}" || fail \
    "could not inspect the formal-evidence Docker daemon"
  "${PYTHON}" "${BACKUP_EVIDENCE_TOOL}" verify-host \
    --docker-context-json "${DOCKER_CONTEXT_EVIDENCE_FILE}" \
    --docker-info-json "${DOCKER_INFO_EVIDENCE_FILE}" >/dev/null || fail \
    "formal backup/restore evidence requires native Linux and rootful Docker"
fi
[[ -f "${RELEASE_BUNDLE_TOOL}" && ! -L "${RELEASE_BUNDLE_TOOL}" ]] || fail \
  "release bundle verifier is missing or unsafe"
"${PYTHON}" "${RELEASE_BUNDLE_TOOL}" verify \
  --bundle-root "${REPOSITORY_ROOT}" \
  --verify-signature >/dev/null || fail \
  "signed release metadata verification failed"
[[ -f "${ENV_FILE}" && ! -L "${ENV_FILE}" ]] || fail \
  "Compose env file is missing or unsafe: ${ENV_FILE}"
backup_id="$(printf '%s' "${verified_manifest_json}" | "${PYTHON}" -c \
  'import json,sys; print(json.load(sys.stdin)["backup_id"])')"
drill_suffix="$("${PYTHON}" -c 'import secrets; print(secrets.token_hex(6))')"
DRILL_PROJECT="auris-flow-restore-drill-${drill_suffix}"
[[ "${DRILL_PROJECT}" =~ ^auris-flow-restore-drill-[0-9a-f]{12}$ ]] || fail \
  "unsafe drill project name"

network_ids_output="$(
  docker --context "${DOCKER_CONTEXT_NAME}" network ls --quiet
)" || fail "could not enumerate Docker networks for restore isolation"
network_ids=()
if [[ -n "${network_ids_output}" ]]; then
  mapfile -t network_ids <<<"${network_ids_output}"
fi
if ((${#network_ids[@]})); then
  docker_networks_json="$(
    docker --context "${DOCKER_CONTEXT_NAME}" network inspect "${network_ids[@]}"
  )" || fail "could not inspect Docker networks for restore isolation"
else
  docker_networks_json='[]'
fi
host_routes_json="$(ip -json -4 route show table all)" || fail \
  "could not inspect host routes for restore isolation"
host_route_cidrs="$(
  printf '%s\n' "${host_routes_json}" | "${PYTHON}" -c '
import ipaddress
import json
import sys

try:
    document = json.load(sys.stdin)
    if not isinstance(document, list) or len(document) > 4096:
        raise ValueError
    routes = []
    for item in document:
        if not isinstance(item, dict):
            raise ValueError
        destination = item.get("dst")
        if destination in (None, "default"):
            continue
        network = ipaddress.ip_network(destination, strict=True)
        if isinstance(network, ipaddress.IPv4Network):
            routes.append(str(network))
except (TypeError, ValueError, json.JSONDecodeError):
    raise SystemExit(2)
print("\n".join(dict.fromkeys(routes)))
'
)" || fail "host routes are invalid for restore network allocation"
network_allocator_arguments=()
if [[ -n "${host_route_cidrs}" ]]; then
  while IFS= read -r host_route_cidr; do
    network_allocator_arguments+=(--host-route "${host_route_cidr}")
  done <<<"${host_route_cidrs}"
fi
network_allocation_json="$(
  printf '%s\n' "${docker_networks_json}" | \
    "${PYTHON}" "${RESTORE_NETWORK_ALLOCATOR}" \
      "${network_allocator_arguments[@]}"
)" || fail "no collision-free restore network is available"
network_allocation_tsv="$(
  printf '%s\n' "${network_allocation_json}" | "${PYTHON}" -c '
import json
import sys

document = json.load(sys.stdin)
if not isinstance(document, dict) or set(document) != {"subnet", "edge_ip"}:
    raise SystemExit(2)
print("{}\t{}".format(document["subnet"], document["edge_ip"]))
'
)" || fail "restore network allocator returned invalid output"
IFS=$'\t' read -r DRILL_INTERNAL_SUBNET DRILL_EDGE_INTERNAL_IP \
  <<<"${network_allocation_tsv}"
[[ -n "${DRILL_INTERNAL_SUBNET}" && -n "${DRILL_EDGE_INTERNAL_IP}" ]] || fail \
  "restore network allocator returned an empty allocation"
export AURIS_INTERNAL_SUBNET="${DRILL_INTERNAL_SUBNET}"
export AURIS_EDGE_INTERNAL_IP="${DRILL_EDGE_INTERNAL_IP}"

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

if [[ -n "${EVIDENCE_OUTPUT}" ]]; then
  RESTORE_STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
fi
compose_drill_with_deadline "${DRILL_RUN_TIMEOUT}" \
  "validate restore drill Compose" config --quiet || fail \
  "Compose configuration is invalid"
printf 'Pulling digest-pinned recovery images for isolated drill %s...\n' "${DRILL_PROJECT}"
compose_drill_with_deadline "${DRILL_PULL_TIMEOUT}" \
  "pull restore drill images" \
  pull mysql db-bootstrap redis minio minio-volume-init minio-bootstrap qdrant bff || fail \
  "could not pull the signed recovery-side images"
compose_drill_with_deadline "${DRILL_RUN_TIMEOUT}" \
  "prepare restore drill object-storage volume" \
  run --rm --no-deps minio-volume-init || fail \
  "restore drill object-storage volume initialization failed"
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
  --manifest-public-key "${MANIFEST_VERIFY_KEY_FILE}"
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
if [[ -n "${SOURCE_LINKAGE_PROOF}" ]]; then
  LINKAGE_PRIVATE_ROOT="$(
    mktemp -d "${TMPDIR:-/tmp}/auris-flow-linkage-verification.XXXXXX"
  )"
  chmod 0700 "${LINKAGE_PRIVATE_ROOT}"
  restored_linkage_proof="${LINKAGE_PRIVATE_ROOT}/restored-proof.json"
  "${RECOVERY_LINKAGE_SCRIPT}" capture \
    --project-name "${DRILL_PROJECT}" \
    --env-file "${ENV_FILE}" \
    --proof-output "${restored_linkage_proof}" || fail \
    "restored MySQL, MinIO, and Qdrant do not form the signed business linkage"
  "${PYTHON}" "${RECOVERY_LINKAGE_TOOL}" validate-proof \
    --input "${restored_linkage_proof}" || fail \
    "restored cross-store linkage proof is invalid"
  cmp -s "${SOURCE_LINKAGE_PROOF}" "${restored_linkage_proof}" || fail \
    "restored cross-store linkage differs from the signed source proof"
fi
if [[ -n "${EVIDENCE_OUTPUT}" ]]; then
  RESTORE_COMPLETED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  EVIDENCE_PREPARED_JSON="$(
    "${PYTHON}" "${BACKUP_EVIDENCE_TOOL}" prepare-input \
      --backup-root "${BACKUP_ROOT}" \
      --verified-manifest-json "${VERIFIED_MANIFEST_FILE}" \
      --docker-context-json "${DOCKER_CONTEXT_EVIDENCE_FILE}" \
      --docker-info-json "${DOCKER_INFO_EVIDENCE_FILE}"
  )" || fail "could not prepare private backup/restore evidence inputs"
  [[ -n "${EVIDENCE_PREPARED_JSON}" ]] || fail \
    "private backup/restore evidence input is empty"
fi

printf 'Isolated restore drill passed for project %s.\n' "${DRILL_PROJECT}"
if [[ "${CLEANUP_ON_SUCCESS}" == true ]]; then
  printf 'Exact-project cleanup requested; removing it through the bounded exit handler.\n'
else
  printf 'Drill project retained for inspection; remove it explicitly with:\n'
  printf '  docker --context %q compose --project-name %q --project-directory %q --env-file %q -f %q down --volumes --remove-orphans\n' \
    "${DOCKER_CONTEXT_NAME}" "${DRILL_PROJECT}" "${PRODUCTION_ROOT}" "${ENV_FILE}" "${COMPOSE_FILE}"
fi
