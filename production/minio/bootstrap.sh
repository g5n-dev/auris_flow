#!/bin/sh
set -eu

MINIO_CLIENT=/opt/auris/minio-client.sh
"${MINIO_CLIENT}" mb --ignore-existing auris/auris-flow >/dev/null
"${MINIO_CLIENT}" anonymous set none auris/auris-flow >/dev/null
"${MINIO_CLIENT}" version enable auris/auris-flow >/dev/null
