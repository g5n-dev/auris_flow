#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE="${ROOT}/doc/architecture/auris-flow-system.mmd"
LIGHT_CONFIG="${ROOT}/doc/architecture/mermaid-light.json"
DARK_CONFIG="${ROOT}/doc/architecture/mermaid-dark.json"
ASSET_DIR="${ROOT}/doc/assets"
IMAGE="ghcr.io/mermaid-js/mermaid-cli/mermaid-cli:11.16.0@sha256:29077c6bd02f14bdfdd5fee552d9c00fe68d4fab3cd84952d21e2d1faf2fadaf"
MODE="${1:-render}"

if [ "${MODE}" != "render" ] && [ "${MODE}" != "--check" ]; then
  echo "Usage: bash scripts/render_readme_architecture.sh [--check]" >&2
  exit 2
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required to render the pinned Mermaid CLI image." >&2
  exit 2
fi

TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/auris-architecture.XXXXXX")"
trap 'rm -rf "${TEMP_DIR}"' EXIT

render_theme() {
  local theme="$1"
  local config="$2"
  local background="$3"

  docker run --rm \
    --user "$(id -u):$(id -g)" \
    --volume "${ROOT}:/workspace:ro" \
    --volume "${TEMP_DIR}:/output" \
    "${IMAGE}" \
    --input /workspace/doc/architecture/auris-flow-system.mmd \
    --output "/output/architecture-${theme}.svg" \
    --configFile "/workspace/${config}" \
    --backgroundColor "${background}" \
    --width 1800 \
    --height 900 \
    --scale 1
}

render_theme light "doc/architecture/mermaid-light.json" "#FFFFFF"
render_theme dark "doc/architecture/mermaid-dark.json" "#0B1220"

python3 "${ROOT}/scripts/verify_readme_architecture.py" \
  --light "${TEMP_DIR}/architecture-light.svg" \
  --dark "${TEMP_DIR}/architecture-dark.svg"

if [ "${MODE}" = "--check" ]; then
  cmp "${TEMP_DIR}/architecture-light.svg" "${ASSET_DIR}/architecture-light.svg"
  cmp "${TEMP_DIR}/architecture-dark.svg" "${ASSET_DIR}/architecture-dark.svg"
  echo "README architecture assets are reproducible."
  exit 0
fi

mkdir -p "${ASSET_DIR}"
cp "${TEMP_DIR}/architecture-light.svg" "${ASSET_DIR}/architecture-light.svg"
cp "${TEMP_DIR}/architecture-dark.svg" "${ASSET_DIR}/architecture-dark.svg"
python3 "${ROOT}/scripts/verify_readme_architecture.py"
echo "README architecture assets rendered from the pinned Mermaid CLI image."
