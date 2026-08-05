#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UI_ROOT="${ROOT}/prototype/auris-flow-ui"
PYTHON_BIN="${PYTHON:-${ROOT}/backend/.venv/bin/python}"
BFF_PORT="${AURIS_AUDIO_IMPORT_GATE_BFF_PORT:?set AURIS_AUDIO_IMPORT_GATE_BFF_PORT}"
RUN_SUFFIX="${AURIS_AUDIO_IMPORT_GATE_RUN_SUFFIX:?set AURIS_AUDIO_IMPORT_GATE_RUN_SUFFIX}"
ARTIFACT="${AURIS_AUDIO_IMPORT_GATE_BROWSER_ARTIFACT:?set AURIS_AUDIO_IMPORT_GATE_BROWSER_ARTIFACT}"
LOG_DIR="${AURIS_AUDIO_IMPORT_GATE_BROWSER_LOG_DIR:?set AURIS_AUDIO_IMPORT_GATE_BROWSER_LOG_DIR}"
READY_TIMEOUT="${AURIS_AUDIO_IMPORT_GATE_UI_READY_TIMEOUT:-120}"

if ! [[ "${BFF_PORT}" =~ ^[1-9][0-9]{0,4}$ ]] || [ "${BFF_PORT}" -gt 65535 ]; then
  echo "Audio import browser gate BFF port is invalid." >&2
  exit 2
fi
if ! [[ "${READY_TIMEOUT}" =~ ^[1-9][0-9]*$ ]]; then
  echo "Audio import browser gate readiness timeout is invalid." >&2
  exit 2
fi
if [ ! -x "${PYTHON_BIN}" ]; then
  echo "Audio import browser gate Python runtime is unavailable." >&2
  exit 2
fi
if [ ! -x "${UI_ROOT}/node_modules/.bin/vite" ] || \
  [ ! -f "${UI_ROOT}/node_modules/playwright/package.json" ]; then
  echo "Audio import browser gate requires installed Vite and Playwright dependencies." >&2
  exit 2
fi

"${PYTHON_BIN}" - "${ROOT}" "${ARTIFACT}" "${LOG_DIR}" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
artifact = Path(sys.argv[2]).resolve()
log_dir = Path(sys.argv[3]).resolve()
if root not in artifact.parents or root not in log_dir.parents:
    raise SystemExit("Audio import browser artifacts must stay inside the repository.")
if artifact.suffix != ".json":
    raise SystemExit("Audio import browser artifact must be JSON.")
PY

free_port() {
  "${PYTHON_BIN}" - <<'PY'
import socket

with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
}

UI_PORT="${AURIS_AUDIO_IMPORT_GATE_UI_PORT:-$(free_port)}"
if ! [[ "${UI_PORT}" =~ ^[1-9][0-9]{0,4}$ ]] || [ "${UI_PORT}" -gt 65535 ]; then
  echo "Audio import browser gate UI port is invalid." >&2
  exit 2
fi

mkdir -p "${LOG_DIR}" "$(dirname "${ARTIFACT}")"
VITE_LOG="${LOG_DIR}/audio-import-browser-vite.log"
E2E_LOG="${LOG_DIR}/audio-import-browser-playwright.log"
BFF_URL="http://127.0.0.1:${BFF_PORT}"
UI_URL="http://127.0.0.1:${UI_PORT}/"
E2E_RUN_ID="audio-import-browser-${RUN_SUFFIX}"
VITE_PID=""

cleanup() {
  local status="$?"
  trap - EXIT INT TERM
  if [ -n "${VITE_PID}" ] && kill -0 "${VITE_PID}" 2>/dev/null; then
    kill -TERM "${VITE_PID}" 2>/dev/null || true
    for _attempt in $(seq 1 30); do
      if ! kill -0 "${VITE_PID}" 2>/dev/null; then
        break
      fi
      sleep 0.1
    done
    if kill -0 "${VITE_PID}" 2>/dev/null; then
      kill -KILL "${VITE_PID}" 2>/dev/null || true
    fi
  fi
  if [ -n "${VITE_PID}" ]; then
    wait "${VITE_PID}" 2>/dev/null || true
  fi
  if [ "${status}" -ne 0 ]; then
    for log_path in "${VITE_LOG}" "${E2E_LOG}"; do
      if [ -f "${log_path}" ]; then
        echo "--- $(basename "${log_path}") (failure tail) ---" >&2
        tail -160 "${log_path}" >&2 || true
      fi
    done
  fi
  exit "${status}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

(
  cd "${UI_ROOT}"
  exec env \
    NODE_ENV=development \
    VITE_DEMO_MODE=false \
    VITE_API_PROXY_TARGET="${BFF_URL}" \
    ./node_modules/.bin/vite \
    --host 127.0.0.1 \
    --port "${UI_PORT}" \
    --strictPort
) >"${VITE_LOG}" 2>&1 &
VITE_PID="$!"

"${PYTHON_BIN}" - "${UI_URL}healthz" "${VITE_PID}" "${READY_TIMEOUT}" <<'PY'
import json
import os
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

url, raw_pid, raw_timeout = sys.argv[1:]
pid = int(raw_pid)
deadline = time.monotonic() + int(raw_timeout)
last_error = "not ready"
while time.monotonic() < deadline:
    try:
        os.kill(pid, 0)
    except OSError as exc:
        raise SystemExit("Vite exited before the browser gate became ready.") from exc
    try:
        with urlopen(url, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if response.status == 200 and payload.get("status") == "ok":
            raise SystemExit(0)
        last_error = "health response was not ready"
    except (OSError, URLError, ValueError) as exc:
        last_error = exc.__class__.__name__
    time.sleep(0.2)
raise SystemExit(f"Timed out waiting for Vite/BFF readiness ({last_error}).")
PY

rm -f -- "${ARTIFACT}"
(
  cd "${ROOT}"
  env \
    AURIS_E2E_ONLY_AUDIO_IMPORT=1 \
    AURIS_REAL_STACK_E2E=1 \
    AURIS_E2E_URL="${UI_URL}" \
    AURIS_E2E_BFF_URL="${BFF_URL}" \
    AURIS_E2E_RUN_ID="${E2E_RUN_ID}" \
    AURIS_E2E_RESULT_PATH="${ARTIFACT}" \
    AURIS_E2E_AUDIO_IMPORT_BASE_URL="https://recordings.audio-import-gate.test:8443" \
    AURIS_E2E_AUDIO_IMPORT_CREDENTIAL_REF="secret://platform/audio-import-gate" \
    AURIS_E2E_AUDIO_IMPORT_PLATFORM_CONNECTION_ID="conn_platform_auth" \
    AURIS_E2E_AUDIO_IMPORT_PLATFORM_TENANT_REF="audio-import-gate-tenant" \
    AURIS_E2E_AUDIO_IMPORT_STORE_SCOPE="BJ-AURORA-001" \
    AURIS_E2E_AUDIO_IMPORT_REQUEST_PATH="/v1/browser-recordings" \
    AURIS_E2E_AUDIO_IMPORT_AUDIO_URL_FIELD="download_url" \
    AURIS_E2E_AUDIO_IMPORT_AGENT_REF_FIELD="employee.badge" \
    AURIS_E2E_AUDIO_IMPORT_INITIAL_WINDOW_START="2026-07-26T00:00" \
    AURIS_E2E_AUDIO_IMPORT_TIMEOUT_MS="${AURIS_E2E_AUDIO_IMPORT_TIMEOUT_MS:-180000}" \
    node prototype/auris-flow-ui/e2e/platform-bff.mjs
) 2>&1 | tee "${E2E_LOG}"

if [ ! -f "${ARTIFACT}" ]; then
  echo "Audio import browser gate did not produce evidence." >&2
  exit 1
fi

(
  cd "${ROOT}"
  env \
    AURIS_REAL_STACK_E2E=1 \
    AURIS_E2E_RUN_ID="${E2E_RUN_ID}" \
    npm --prefix prototype/auris-flow-ui run e2e:bff:check -- "${ARTIFACT}"
)

"${PYTHON_BIN}" - "${ARTIFACT}" <<'PY'
import json
import sys
from pathlib import Path

artifact = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
flow = artifact.get("audioImportClosedLoop")
vertical = artifact.get("audioIntelligenceReviewClosedLoop")
tenant_pull = artifact.get("tenantAudioImportPull")
profile = artifact.get("executionProfile")
if (
    artifact.get("schema_version") != "auris.audio-import-browser-e2e.v2"
    or artifact.get("status") != "ok"
    or artifact.get("mode") != "audio-import-only"
    or not isinstance(flow, dict)
    or not isinstance(vertical, dict)
    or not isinstance(tenant_pull, dict)
    or profile
    != {
        "realStack": True,
        "platformSource": "https",
        "inferenceProvider": "https",
        "dagster": "real",
        "objectStorage": "real",
        "uiEvidencePolicy": "browser-clicks-and-bff-readback",
    }
    or flow.get("status") != "succeeded"
    or flow.get("executionMode") != "production"
    or flow.get("previewCount") != 3
    or flow.get("total") != 3
    or flow.get("succeeded") != 3
    or flow.get("failed") != 0
    or flow.get("playbackGrantStatus") != 201
    or flow.get("playbackStatus") != 206
    or flow.get("playbackUiBound") is not True
    or flow.get("playbackRangeVerified") is not True
    or flow.get("pageRefreshRecovered") is not True
    or flow.get("rootTraceReadable") is not True
    or flow.get("legacyPlatformSyncRequests") != 0
    or vertical.get("audioSessionId") != flow.get("audioSessionId")
    or vertical.get("rootTraceId") != flow.get("rootTraceId")
    or vertical.get("intelligenceRunStatus") != "success"
    or vertical.get("evidenceStatus") != "ready"
    or vertical.get("reviewQueue") != "audio_evidence_review"
    or vertical.get("reviewDecision") != "modified"
    or vertical.get("reviewStatus") != "success"
    or vertical.get("taskReadbackMatched") is not True
    or vertical.get("evidenceReadbackMatched") is not True
    or vertical.get("affectedObjectsReadBack") is not True
    or vertical.get("traceRootMatched") is not True
    or vertical.get("noSeedSwitch") is not True
    or tenant_pull.get("executionMode") != "production"
    or tenant_pull.get("taskVersionId") != flow.get("taskVersionId")
    or tenant_pull.get("legacyPlatformSyncRequests") != 0
    or any(
        artifact.get(name) != []
        for name in (
            "consoleErrors",
            "pageErrors",
            "requestFailures",
            "failedResponses",
        )
    )
):
    raise SystemExit("Audio import browser evidence is incomplete.")
PY

echo "verify_audio_import_browser_e2e ok: ${ARTIFACT}"
