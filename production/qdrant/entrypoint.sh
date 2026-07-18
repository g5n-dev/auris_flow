#!/bin/sh
set -eu
export QDRANT__SERVICE__API_KEY="$(cat /run/secrets/qdrant_api_key)"
export QDRANT__SERVICE__ENABLE_TLS=0
exec /qdrant/qdrant
