#!/bin/sh
set -eu

access_key="$(cat /run/secrets/object_storage_access_key)"
secret_key="$(cat /run/secrets/object_storage_secret_key)"
mc alias set auris http://minio:9000 "$access_key" "$secret_key" >/dev/null
mc mb --ignore-existing auris/auris-flow >/dev/null
mc anonymous set none auris/auris-flow >/dev/null
mc version enable auris/auris-flow >/dev/null
