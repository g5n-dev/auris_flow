#!/bin/sh
set -eu

umask 077

read_secret_file() {
  secret_path="$1"
  secret_name="$2"

  if ! secret_value="$(python - "$secret_path" <<'PY' 2>/dev/null
import stat
import sys
from pathlib import Path

path = Path(sys.argv[1])
metadata = path.stat()
if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0 or metadata.st_size > 65_536:
    raise SystemExit(1)
raw = path.read_bytes()
if b"\x00" in raw:
    raise SystemExit(1)
value = raw.decode("utf-8")
if value.endswith("\n"):
    value = value[:-1]
if value.endswith("\r"):
    value = value[:-1]
if not value or "\n" in value or "\r" in value:
    raise SystemExit(1)
sys.stdout.write(value)
PY
  )"; then
    echo "Auris Flow Dagster configuration error: ${secret_name} secret file is invalid" >&2
    exit 78
  fi
  printf '%s' "$secret_value"
}

if [ -n "${DAGSTER_MYSQL_URL:-}" ] && [ -e /run/secrets/dagster_database_url ]; then
  echo "Auris Flow Dagster configuration error: database secret has two sources" >&2
  exit 78
fi
if [ -z "${DAGSTER_MYSQL_URL:-}" ]; then
  DAGSTER_MYSQL_URL="$(read_secret_file /run/secrets/dagster_database_url dagster_database_url)"
  export DAGSTER_MYSQL_URL
fi

# Compose may explicitly invoke this entrypoint for role-oriented commands.
if [ "${1:-}" = "/opt/auris/dagster-entrypoint.sh" ]; then
  shift
fi

case "${1:-webserver}" in
  webserver)
    shift || true
    exec dagster-webserver \
      --host 0.0.0.0 \
      --port "${DAGSTER_WEBSERVER_PORT:-3000}" \
      --workspace /opt/dagster/app/workspace.yaml \
      "$@"
    ;;
  daemon)
    shift || true
    exec dagster-daemon run \
      --workspace /opt/dagster/app/workspace.yaml \
      "$@"
    ;;
  health)
    shift || true
    exec dagster-daemon liveness-check "$@"
    ;;
  grpc)
    shift || true
    exec dagster api grpc \
      --host 0.0.0.0 \
      --port "${DAGSTER_GRPC_PORT:-4000}" \
      --module-name auris_flow_dagster.definitions \
      "$@"
    ;;
  *)
    exec "$@"
    ;;
esac
