#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

npm --prefix prototype/auris-flow-ui run architecture:test
npm --prefix prototype/auris-flow-ui run architecture:final
npm --prefix prototype/auris-flow-ui run build
npm --prefix prototype/auris-flow-ui run bundle:check
npm --prefix prototype/auris-flow-ui run e2e:preview:check
npm --prefix prototype/auris-flow-ui run e2e:ui

echo "verify_frontend ok"
