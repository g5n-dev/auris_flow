#!/bin/sh
set -eu

umask 077

if [ "$#" -eq 0 ]; then
  printf 'minio client failed: an mc command is required\n' >&2
  exit 2
fi

MC_CONFIG_PARENT="${TMPDIR:-/tmp}"
MC_CONFIG_DIR="$(mktemp -d "${MC_CONFIG_PARENT%/}/auris-flow-mc.XXXXXX")"

cleanup_mc_config() {
  case "${MC_CONFIG_DIR}" in
    "${MC_CONFIG_PARENT%/}"/auris-flow-mc.*)
      rm -rf -- "${MC_CONFIG_DIR}"
      ;;
    *)
      printf 'minio client warning: refusing to clean unexpected config path\n' >&2
      ;;
  esac
}
trap cleanup_mc_config EXIT

OBJECT_STORAGE_ACCESS_KEY_FILE="${OBJECT_STORAGE_ACCESS_KEY_FILE:-/run/secrets/object_storage_access_key}"
OBJECT_STORAGE_SECRET_KEY_FILE="${OBJECT_STORAGE_SECRET_KEY_FILE:-/run/secrets/object_storage_secret_key}"
access_key="$(cat "${OBJECT_STORAGE_ACCESS_KEY_FILE}")"
secret_key="$(cat "${OBJECT_STORAGE_SECRET_KEY_FILE}")"

if [ -z "${access_key}" ] || [ -z "${secret_key}" ]; then
  printf 'minio client failed: object-storage credentials are empty\n' >&2
  exit 2
fi
case "${access_key}${secret_key}" in
  *[!A-Za-z0-9._~-]*)
    printf 'minio client failed: object-storage credentials contain unsupported URI characters\n' >&2
    exit 2
    ;;
esac

# MC_HOST_auris is the documented environment-backed alias. Keeping credentials
# in the child environment avoids exposing them in `mc alias set` process args.
MC_HOST_auris="http://${access_key}:${secret_key}@minio:9000"
export MC_CONFIG_DIR MC_HOST_auris
unset access_key secret_key

mc --config-dir "${MC_CONFIG_DIR}" "$@"
