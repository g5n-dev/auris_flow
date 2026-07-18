#!/bin/sh
set -eu
export MINIO_ROOT_USER="$(cat /run/secrets/object_storage_access_key)"
export MINIO_ROOT_PASSWORD="$(cat /run/secrets/object_storage_secret_key)"
exec minio "$@"
