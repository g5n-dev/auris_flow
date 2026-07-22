#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OBSERVABILITY_DIR="${ROOT}/production/observability"
PROMTOOL_IMAGE="prom/prometheus:v3.4.1@sha256:9abc6cf6aea7710d163dbb28d8eeb7dc5baef01e38fa4cd146a406dd9f07f70d"

if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  echo "Docker Engine is required for the pinned promtool rule gate." >&2
  exit 2
fi

run_promtool() {
  docker run --rm --pull always \
    --network none \
    --read-only \
    --tmpfs /tmp:rw,noexec,nosuid,size=64m \
    --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --mount "type=bind,src=${OBSERVABILITY_DIR},dst=/etc/prometheus,readonly" \
    --workdir /etc/prometheus \
    --entrypoint /bin/promtool \
    "${PROMTOOL_IMAGE}" "$@"
}

run_promtool check config prometheus.yaml
run_promtool check rules alerts.yaml
run_promtool test rules alerts.test.yaml
