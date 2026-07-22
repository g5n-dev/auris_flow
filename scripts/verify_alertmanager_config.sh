#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OBSERVABILITY_DIR="${ROOT}/production/observability"
ALERTMANAGER_IMAGE="prom/alertmanager:v0.28.1@sha256:27c475db5fb156cab31d5c18a4251ac7ed567746a2483ff264516437a39b15ba"

if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  echo "Docker Engine is required for the pinned amtool configuration gate." >&2
  exit 2
fi

run_amtool() {
  docker run --rm --pull always \
    --network none \
    --read-only \
    --tmpfs /tmp:rw,noexec,nosuid,size=16m \
    --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --mount "type=bind,src=${OBSERVABILITY_DIR},dst=/etc/alertmanager,readonly" \
    --workdir /etc/alertmanager \
    --entrypoint /bin/amtool \
    "${ALERTMANAGER_IMAGE}" "$@"
}

run_amtool check-config --enable-feature=utf8-strict-mode alertmanager.yaml

# The wrapper must reject startup before Alertmanager sees its arguments when
# the operator-supplied URL secret is absent. --version makes an accidental
# pass terminate immediately instead of leaving a daemon behind.
if docker run --rm --pull never \
  --network none \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=16m \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --mount "type=bind,src=${OBSERVABILITY_DIR},dst=/etc/alertmanager,readonly" \
  --entrypoint /bin/sh \
  "${ALERTMANAGER_IMAGE}" \
  /etc/alertmanager/alertmanager-entrypoint.sh --version \
  >/dev/null 2>&1; then
  echo "alertmanager missing-secret probe unexpectedly succeeded" >&2
  exit 1
fi
