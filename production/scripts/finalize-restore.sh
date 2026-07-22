#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRODUCTION_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPOSITORY_ROOT="$(cd "${PRODUCTION_ROOT}/.." && pwd)"
COMPOSE_FILE="${PRODUCTION_ROOT}/compose.yaml"
BACKUP_TOOLS="${PRODUCTION_ROOT}/backup"
RESTORE_STATE_TOOL="${BACKUP_TOOLS}/restore_state.py"
RELEASE_BUNDLE_TOOL="${REPOSITORY_ROOT}/scripts/release_bundle.py"
PYTHON="${PYTHON:-python3}"
ENV_FILE="${AURIS_COMPOSE_ENV_FILE:-${PRODUCTION_ROOT}/.env}"
SECRETS_DIR="${AURIS_SECRETS_DIR:-${PRODUCTION_ROOT}/secrets}"
MANIFEST_VERIFY_KEY_FILE="${AURIS_BACKUP_MANIFEST_VERIFY_KEY_FILE:-${SECRETS_DIR}/backup_manifest_signing_public_key.pem}"
RESTORE_ATTESTATION_PRIVATE_KEY_FILE="${AURIS_RESTORE_ATTESTATION_SIGNING_PRIVATE_KEY_FILE:-${SECRETS_DIR}/restore_attestation_signing_private_key.pem}"
RESTORE_ATTESTATION_VERIFY_KEY_FILE="${AURIS_RESTORE_ATTESTATION_VERIFY_KEY_FILE:-${SECRETS_DIR}/restore_attestation_signing_public_key.pem}"
PRODUCTION_PROJECT_NAME="auris-flow"
DOCKER_CONTEXT_NAME="default"
BACKUP_ROOT=""
STATE_FILE=""
CONFIRMATION=""
ALLOW_RELEASE_MIGRATION_FROM=""
RESTORE_STEP="preflight"
SNAPSHOT_ROOT=""
QDRANT_EVIDENCE_FILE=""
RUNNING_IMAGES_EVIDENCE_FILE=""
READYZ_EVIDENCE_FILE=""
ENV_SNAPSHOT_ROOT=""
ENV_SNAPSHOT_FILE=""

usage() {
  cat <<'USAGE'
Usage: production/scripts/finalize-restore.sh --backup ABSOLUTE_DIR \
       --state ABSOLUTE_STATE_JSON --confirm BACKUP_ID [options]

Options:
  --env-file FILE
  --manifest-public-key FILE
  --restore-attestation-private-key FILE
  --restore-attestation-public-key FILE
  --project-name NAME
  --docker-context NAME
  --allow-release-migration-from COMMIT
  -h, --help

This command is the only supported transition from pending Qdrant rebuild to
complete. It re-verifies the externally signed backup, exact pending identity,
live Qdrant semantic fingerprints, signed running images, and BFF readiness.
USAGE
}

fail() {
  printf 'restore finalize failed at %s: %s\n' "${RESTORE_STEP}" "$1" >&2
  exit 2
}

cleanup() {
  local status="$?"
  trap - EXIT
  if [[ -n "${QDRANT_EVIDENCE_FILE}" && -f "${QDRANT_EVIDENCE_FILE}" ]]; then
    rm -f -- "${QDRANT_EVIDENCE_FILE}" || status=1
  fi
  if [[ -n "${RUNNING_IMAGES_EVIDENCE_FILE}" && -f "${RUNNING_IMAGES_EVIDENCE_FILE}" ]]; then
    rm -f -- "${RUNNING_IMAGES_EVIDENCE_FILE}" || status=1
  fi
  if [[ -n "${READYZ_EVIDENCE_FILE}" && -f "${READYZ_EVIDENCE_FILE}" ]]; then
    rm -f -- "${READYZ_EVIDENCE_FILE}" || status=1
  fi
  if [[ -n "${ENV_SNAPSHOT_FILE}" && -f "${ENV_SNAPSHOT_FILE}" ]]; then
    rm -f -- "${ENV_SNAPSHOT_FILE}" || status=1
  fi
  if [[ -n "${ENV_SNAPSHOT_ROOT}" && -d "${ENV_SNAPSHOT_ROOT}" && ! -L "${ENV_SNAPSHOT_ROOT}" ]]; then
    rmdir -- "${ENV_SNAPSHOT_ROOT}" || status=1
  fi
  if [[ -n "${SNAPSHOT_ROOT}" && -d "${SNAPSHOT_ROOT}" && ! -L "${SNAPSHOT_ROOT}" ]]; then
    "${PYTHON}" "${BACKUP_TOOLS}/manifest.py" destroy-snapshot \
      --snapshot-root "${SNAPSHOT_ROOT}" >/dev/null 2>&1 || status=1
  fi
  exit "${status}"
}
trap cleanup EXIT

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
    --state)
      (($# >= 2)) || fail "--state requires a value"
      STATE_FILE="$2"
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
    --restore-attestation-private-key)
      (($# >= 2)) || fail "--restore-attestation-private-key requires a value"
      RESTORE_ATTESTATION_PRIVATE_KEY_FILE="$2"
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

for command_name in docker mktemp openssl cosign "${PYTHON}"; do
  command -v "${command_name}" >/dev/null 2>&1 || fail \
    "required command not found: ${command_name}"
done
for path_value in "${BACKUP_ROOT}" "${STATE_FILE}" "${ENV_FILE}" \
  "${MANIFEST_VERIFY_KEY_FILE}" "${RESTORE_ATTESTATION_PRIVATE_KEY_FILE}" \
  "${RESTORE_ATTESTATION_VERIFY_KEY_FILE}"; do
  has_control_character "${path_value}" && fail "input path contains a control character"
  [[ "${path_value}" == /* ]] || fail "all input paths must be absolute"
done
[[ -d "${BACKUP_ROOT}" && ! -L "${BACKUP_ROOT}" ]] || fail \
  "backup root must be a real directory"
[[ -f "${STATE_FILE}" && ! -L "${STATE_FILE}" ]] || fail \
  "pending restore state must be a regular file"
[[ -f "${ENV_FILE}" && ! -L "${ENV_FILE}" ]] || fail \
  "Compose env file is missing or unsafe"
[[ -f "${MANIFEST_VERIFY_KEY_FILE}" && ! -L "${MANIFEST_VERIFY_KEY_FILE}" ]] || fail \
  "backup manifest public trust-key file is missing or unsafe"
[[ -f "${RESTORE_ATTESTATION_PRIVATE_KEY_FILE}" && \
  ! -L "${RESTORE_ATTESTATION_PRIVATE_KEY_FILE}" ]] || fail \
  "restore attestation private signing-key file is missing or unsafe"
[[ -f "${RESTORE_ATTESTATION_VERIFY_KEY_FILE}" && \
  ! -L "${RESTORE_ATTESTATION_VERIFY_KEY_FILE}" ]] || fail \
  "restore attestation public trust-key file is missing or unsafe"
MANIFEST_VERIFY_KEY_FILE="$(cd "$(dirname "${MANIFEST_VERIFY_KEY_FILE}")" && pwd -P)/$(basename "${MANIFEST_VERIFY_KEY_FILE}")"
RESTORE_ATTESTATION_PRIVATE_KEY_FILE="$(cd "$(dirname "${RESTORE_ATTESTATION_PRIVATE_KEY_FILE}")" && pwd -P)/$(basename "${RESTORE_ATTESTATION_PRIVATE_KEY_FILE}")"
RESTORE_ATTESTATION_VERIFY_KEY_FILE="$(cd "$(dirname "${RESTORE_ATTESTATION_VERIFY_KEY_FILE}")" && pwd -P)/$(basename "${RESTORE_ATTESTATION_VERIFY_KEY_FILE}")"
[[ -f "${RESTORE_STATE_TOOL}" && ! -L "${RESTORE_STATE_TOOL}" ]] || fail \
  "restore state transition tool is missing or unsafe"
[[ "${PRODUCTION_PROJECT_NAME}" =~ ^[a-z0-9][a-z0-9_-]{0,62}$ ]] || fail \
  "Compose project name is invalid"
[[ "${DOCKER_CONTEXT_NAME}" =~ ^[A-Za-z0-9_.-]+$ ]] || fail \
  "Docker context name is invalid"
if [[ -n "${ALLOW_RELEASE_MIGRATION_FROM}" && \
  ! "${ALLOW_RELEASE_MIGRATION_FROM}" =~ ^[0-9a-f]{40}$ && \
  ! "${ALLOW_RELEASE_MIGRATION_FROM}" =~ ^[0-9a-f]{64}$ ]]; then
  fail "--allow-release-migration-from must be a complete lowercase Git id"
fi
[[ -z "${DOCKER_HOST:-}" && -z "${DOCKER_CONTEXT:-}" && \
  -z "${COMPOSE_PROJECT_NAME:-}" ]] || fail \
  "DOCKER_HOST, DOCKER_CONTEXT and COMPOSE_PROJECT_NAME overrides are forbidden"

BACKUP_ROOT="$(cd "${BACKUP_ROOT}" && pwd -P)"
STATE_FILE="$(cd "$(dirname "${STATE_FILE}")" && pwd -P)/$(basename "${STATE_FILE}")"
paths_overlap "${MANIFEST_VERIFY_KEY_FILE}" "${BACKUP_ROOT}" && fail \
  "backup manifest public trust key must be external to the backup"
paths_overlap "${RESTORE_ATTESTATION_PRIVATE_KEY_FILE}" "${BACKUP_ROOT}" && fail \
  "restore attestation private signing key must be external to the backup"
paths_overlap "${RESTORE_ATTESTATION_VERIFY_KEY_FILE}" "${BACKUP_ROOT}" && fail \
  "restore attestation public trust key must be external to the backup"
paths_overlap "${BACKUP_ROOT}" "${REPOSITORY_ROOT}" && fail \
  "backup path must not overlap the release bundle"
paths_overlap "$(dirname "${STATE_FILE}")" "${BACKUP_ROOT}" && fail \
  "pending state and backup root must not contain one another"

RESTORE_STEP="snapshot-compose-environment"
ENV_SNAPSHOT_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/auris-flow-finalize-env.XXXXXX")"
ENV_SNAPSHOT_FILE="${ENV_SNAPSHOT_ROOT}/compose.env"
"${PYTHON}" "${RESTORE_STATE_TOOL}" snapshot-private-file \
  --source "${ENV_FILE}" --output "${ENV_SNAPSHOT_FILE}" >/dev/null || fail \
  "could not create an owner-only stable Compose environment snapshot"
ENV_FILE="${ENV_SNAPSHOT_FILE}"

RESTORE_STEP="snapshot-signed-backup"
SNAPSHOT_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/auris-flow-finalize-snapshot.XXXXXX")"
"${PYTHON}" "${BACKUP_TOOLS}/manifest.py" snapshot \
  --source "${BACKUP_ROOT}" \
  --snapshot-root "${SNAPSHOT_ROOT}" \
  --public-key "${MANIFEST_VERIFY_KEY_FILE}" >/dev/null || fail \
  "could not create private signed-backup snapshot"
BACKUP_ROOT="${SNAPSHOT_ROOT}/backup"
verified_manifest_json="$("${PYTHON}" "${BACKUP_TOOLS}/manifest.py" verify \
  --root "${BACKUP_ROOT}" --public-key "${MANIFEST_VERIFY_KEY_FILE}")" || fail \
  "external manifest signature or artifact verification failed"
backup_id="$(printf '%s' "${verified_manifest_json}" | "${PYTHON}" -c \
  'import json,sys; print(json.load(sys.stdin)["backup_id"])')"
backup_commit="$(printf '%s' "${verified_manifest_json}" | "${PYTHON}" -c \
  'import json,sys; print(json.load(sys.stdin)["git_commit"])')"
backup_release="$(printf '%s' "${verified_manifest_json}" | "${PYTHON}" -c \
  'import json,sys; print(json.load(sys.stdin)["release_version"])')"
backup_metadata_sha256="$(printf '%s' "${verified_manifest_json}" | "${PYTHON}" -c \
  'import json,sys; print(json.load(sys.stdin)["release_metadata_sha256"])')"
backup_manifest_sha256="$(printf '%s' "${verified_manifest_json}" | "${PYTHON}" -c \
  'import json,sys; print(json.load(sys.stdin)["manifest_sha256"])')"
backup_signing_key_id="$(printf '%s' "${verified_manifest_json}" | "${PYTHON}" -c \
  'import json,sys; print(json.load(sys.stdin)["signing_key_id"])')"
restore_attestation_key_id="$(printf '%s' "${verified_manifest_json}" | "${PYTHON}" -c \
  'import json,sys; print(json.load(sys.stdin)["restore_attestation_key_id"])')"
[[ "${CONFIRMATION}" == "${backup_id}" ]] || fail \
  "confirmation must exactly equal backup id ${backup_id}"

RESTORE_STEP="signed-release-compatibility"
release_identity="$("${PYTHON}" "${RELEASE_BUNDLE_TOOL}" identity \
  --bundle-root "${REPOSITORY_ROOT}" --verify-signature)" || fail \
  "signed release metadata verification failed"
IFS=$'\t' read -r current_commit current_release current_metadata_sha256 \
  _current_compose_sha256 _current_image_lock_sha256 <<<"${release_identity}"
if [[ "${current_commit}" != "${backup_commit}" || \
  "${current_release}" != "${backup_release}" || \
  "${current_metadata_sha256}" != "${backup_metadata_sha256}" ]]; then
  [[ "${ALLOW_RELEASE_MIGRATION_FROM}" == "${backup_commit}" ]] || fail \
    "backup release differs from installed release without exact acknowledgement"
  "${PYTHON}" "${RELEASE_BUNDLE_TOOL}" verify-restore-source \
    --bundle-root "${REPOSITORY_ROOT}" \
    --backup-release-tag "${backup_release}" \
    --backup-source-commit "${backup_commit}" \
    --backup-metadata-sha256 "${backup_metadata_sha256}" \
    --verify-signature >/dev/null || fail \
    "backup release is outside the signed restore compatibility policy"
fi

RESTORE_STEP="pending-state-identity"
"${PYTHON}" "${RESTORE_STATE_TOOL}" require-pending \
  --state "${STATE_FILE}" \
  --backup-id "${backup_id}" \
  --source-commit "${backup_commit}" \
  --manifest-sha256 "${backup_manifest_sha256}" \
  --manifest-signing-key-id "${backup_signing_key_id}" \
  --attestation-key-id "${restore_attestation_key_id}" >/dev/null || fail \
  "restore state is not the pending state for this signed backup"

RESTORE_STEP="restore-attestation-key-preflight"
restore_attestation_key_json="$("${PYTHON}" "${BACKUP_TOOLS}/manifest.py" verify-key-pair \
  --private-key "${RESTORE_ATTESTATION_PRIVATE_KEY_FILE}" \
  --public-key "${RESTORE_ATTESTATION_VERIFY_KEY_FILE}")" || fail \
  "restore attestation Ed25519 signing key pair is invalid"
provided_restore_attestation_key_id="$(printf '%s' "${restore_attestation_key_json}" | "${PYTHON}" -c \
  'import json,sys; print(json.load(sys.stdin)["key_id"])')"
[[ "${provided_restore_attestation_key_id}" == "${restore_attestation_key_id}" ]] || fail \
  "restore attestation key pair does not match the signed manifest delegation"
[[ "${backup_signing_key_id}" != "${restore_attestation_key_id}" ]] || fail \
  "manifest signing and restore attestation key roles are not separated"

compose() {
  COMPOSE_PROJECT_NAME="${PRODUCTION_PROJECT_NAME}" \
    docker --context "${DOCKER_CONTEXT_NAME}" compose \
    --project-name "${PRODUCTION_PROJECT_NAME}" \
    --project-directory "${PRODUCTION_ROOT}" \
    --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" "$@"
}

RESTORE_STEP="running-image-and-service-preflight"
compose config --quiet || fail "Compose configuration is invalid"
running_services="$(compose ps --status running --services)"
for required_service in mysql minio qdrant redis dagster-code dagster-daemon bff worker edge; do
  printf '%s\n' "${running_services}" | grep -Fxq "${required_service}" || fail \
    "required governed-finalize service is not running: ${required_service}"
done
RUNNING_IMAGES_EVIDENCE_FILE="$(mktemp "${TMPDIR:-/tmp}/auris-running-images-finalize.XXXXXX")"
"${PYTHON}" "${RELEASE_BUNDLE_TOOL}" verify-running-images \
  --bundle-root "${REPOSITORY_ROOT}" \
  --project-directory "${PRODUCTION_ROOT}" \
  --env-file "${ENV_FILE}" \
  --project-name "${PRODUCTION_PROJECT_NAME}" \
  --docker-context "${DOCKER_CONTEXT_NAME}" \
  --all-running-release-services --verify-signature >"${RUNNING_IMAGES_EVIDENCE_FILE}" || fail \
  "running services do not match signed release images"

outbox_active_count() {
  compose exec -T mysql sh -ec \
    'MYSQL_PWD="$(cat /run/secrets/mysql_root_password)" exec mysql --protocol=socket --batch --skip-column-names -uroot -e "SELECT COUNT(*) FROM auris_flow.outbox_events WHERE status IN ('"'"'pending'"'"','"'"'processing'"'"');"'
}

RESTORE_STEP="bff-readiness"
READYZ_EVIDENCE_FILE="$(mktemp "${TMPDIR:-/tmp}/auris-readyz-finalize.XXXXXX")"
compose exec -T bff python -c \
  'import json,urllib.request
class Reject(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs): return None
request=urllib.request.Request("http://bff:8000/readyz",headers={"Accept":"application/json"},method="GET")
with urllib.request.build_opener(Reject()).open(request,timeout=10) as response:
 status=response.status; raw=response.read(65537)
if status != 200 or len(raw) > 65536: raise SystemExit(1)
payload=json.loads(raw)
data=payload.get("data") if isinstance(payload,dict) else None
checks=data.get("checks") if isinstance(data,dict) else None
required=data.get("required_checks") if isinstance(data,dict) else None
missing=data.get("missing_required") if isinstance(data,dict) else None
if payload.get("status") != "ok" or not isinstance(checks,dict) or not isinstance(required,list) or missing != {}: raise SystemExit(1)
if any(not isinstance(name,str) or checks.get(name) != "ok" for name in required): raise SystemExit(1)
print(json.dumps(payload,sort_keys=True,separators=(",",":")))' \
  >"${READYZ_EVIDENCE_FILE}" || fail \
  "BFF strict readiness did not pass before the governed write fence"

RESTORE_STEP="freeze-new-write-ingress"
compose stop --timeout 60 edge dagster-daemon bff dagster-code || fail \
  "could not stop every write producer before governed finalize"

RESTORE_STEP="drain-outbox-before-freeze"
outbox_deadline=$((SECONDS + 120))
while true; do
  active_outbox_count="$(outbox_active_count)" || fail \
    "could not inspect authoritative Outbox state"
  [[ "${active_outbox_count}" =~ ^[0-9]+$ ]] || fail \
    "authoritative Outbox count is invalid"
  [[ "${active_outbox_count}" -eq 0 ]] && break
  ((SECONDS < outbox_deadline)) || fail \
    "Outbox did not drain before the governed finalize fence"
  sleep 2
done

RESTORE_STEP="freeze-all-qdrant-writers"
compose stop --timeout 60 worker || fail \
  "could not stop the final Outbox/Qdrant writer"

RESTORE_STEP="final-outbox-write-fence"
active_outbox_count="$(outbox_active_count)" || fail \
  "could not recheck authoritative Outbox state after writer freeze"
[[ "${active_outbox_count}" == "0" ]] || fail \
  "Outbox changed while the write plane was being frozen"

RESTORE_STEP="qdrant-semantic-finalize"
QDRANT_EVIDENCE_FILE="$(mktemp "${TMPDIR:-/tmp}/auris-qdrant-finalize.XXXXXX")"
compose run --rm --no-deps \
  --user "$(id -u):$(id -g)" \
  -v "${BACKUP_ROOT}:/backup:ro" \
  -v "${BACKUP_TOOLS}/qdrant_snapshots.py:/opt/auris/qdrant-snapshots.py:ro" \
  --entrypoint python qdrant-backup-tool \
  /opt/auris/qdrant-snapshots.py verify-semantics --input /backup/qdrant \
  >"${QDRANT_EVIDENCE_FILE}" || fail \
  "governed Qdrant rebuild does not match signed backup semantics"

RESTORE_STEP="post-qdrant-write-fence"
active_outbox_count="$(outbox_active_count)" || fail \
  "could not recheck authoritative Outbox state after semantic verification"
[[ "${active_outbox_count}" == "0" ]] || fail \
  "Outbox changed after every application writer was stopped"
observed_at_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

RESTORE_STEP="complete-transition"
completed_at_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
"${PYTHON}" "${RESTORE_STATE_TOOL}" finalize \
  --state "${STATE_FILE}" \
  --backup-id "${backup_id}" \
  --source-commit "${backup_commit}" \
  --manifest-sha256 "${backup_manifest_sha256}" \
  --manifest-signing-key-id "${backup_signing_key_id}" \
  --attestation-key-id "${restore_attestation_key_id}" \
  --qdrant-evidence "${QDRANT_EVIDENCE_FILE}" \
  --running-images-evidence "${RUNNING_IMAGES_EVIDENCE_FILE}" \
  --readyz-evidence "${READYZ_EVIDENCE_FILE}" \
  --private-key "${RESTORE_ATTESTATION_PRIVATE_KEY_FILE}" \
  --public-key "${RESTORE_ATTESTATION_VERIFY_KEY_FILE}" \
  --observed-at-utc "${observed_at_utc}" \
  --completed-at-utc "${completed_at_utc}" >/dev/null || fail \
  "could not atomically finalize the pending restore state"
"${PYTHON}" "${RESTORE_STATE_TOOL}" verify-complete \
  --state "${STATE_FILE}" \
  --backup-id "${backup_id}" \
  --source-commit "${backup_commit}" \
  --manifest-sha256 "${backup_manifest_sha256}" \
  --manifest-signing-key-id "${backup_signing_key_id}" \
  --attestation-key-id "${restore_attestation_key_id}" \
  --public-key "${RESTORE_ATTESTATION_VERIFY_KEY_FILE}" >/dev/null || fail \
  "signed restore completion attestation verification failed"

printf 'Governed Qdrant rebuild finalized for %s. State: %s\n' \
  "${backup_id}" "${STATE_FILE}"
printf '%s\n' \
  'Write plane remains fenced: edge, worker, and dagster-daemon are stopped; inspect the signed state before restarting them.'
