#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -n "${PYTHON:-}" ]; then
  PYTHON_BIN="${PYTHON}"
elif [ -x "${ROOT}/backend/.venv/bin/python" ]; then
  PYTHON_BIN="${ROOT}/backend/.venv/bin/python"
else
  PYTHON_BIN="$(command -v python3)"
fi
TEMP_ROOT="${TMPDIR:-/tmp}"
GATE_DIR="$(mktemp -d "${TEMP_ROOT%/}/auris-prod-mysql-gate.XXXXXX")"
PROJECT_NAME="auris-prod-mysql-gate-$$-${RANDOM}"
SECRETS_DIR="${GATE_DIR}/secrets"
METRICS_DIR="${GATE_DIR}/runtime-metrics"

export AURIS_SECRETS_DIR="${SECRETS_DIR}"
export AURIS_RUNTIME_METRICS_DIR="${METRICS_DIR}"
export AURIS_PUBLIC_HOST="mysql-gate.auris.invalid"
export AURIS_EXTERNAL_CALLBACK_URL="https://callback.auris.invalid/complete"
export AURIS_EXTERNAL_CALLBACK_HOST="callback.auris.invalid"
export AURIS_EMBEDDING_ENDPOINT="https://embedding.auris.invalid/v1/embeddings"
export AURIS_EMBEDDING_MODEL="mysql-gate-placeholder"

COMPOSE=(
  docker compose
  --project-name "${PROJECT_NAME}"
  --project-directory "${ROOT}/production"
  --env-file "${ROOT}/production/.env.example"
  --file "${ROOT}/production/compose.yaml"
)

compose_with_deadline() {
  local timeout_seconds="$1"
  local label="$2"
  shift 2
  "${PYTHON_BIN}" "${ROOT}/scripts/run_with_deadline.py" \
    --timeout-seconds "${timeout_seconds}" \
    --label "${label}" -- \
    "${COMPOSE[@]}" "$@"
}

cleanup() {
  local status="$1" cleanup_failed=0
  trap - EXIT INT TERM
  set +e
  if [ "${status}" -ne 0 ]; then
    compose_with_deadline 30 "production MySQL gate diagnostics" \
      ps --all >&2
    compose_with_deadline 30 "production MySQL gate diagnostics" \
      logs --no-color mysql db-bootstrap >&2
  fi
  if ! compose_with_deadline 60 "production MySQL gate teardown" \
    down --volumes --remove-orphans >/dev/null 2>&1; then
    echo "production MySQL gate teardown failed" >&2
    cleanup_failed=1
  fi
  case "${GATE_DIR}" in
    "${TEMP_ROOT%/}"/auris-prod-mysql-gate.*)
      if ! rm -rf -- "${GATE_DIR}"; then
        echo "could not remove production MySQL gate path: ${GATE_DIR}" >&2
        cleanup_failed=1
      fi
      ;;
    *)
      echo "refusing to remove unexpected production MySQL gate path: ${GATE_DIR}" >&2
      cleanup_failed=1
      ;;
  esac
  if [ "${status}" -eq 0 ] && [ "${cleanup_failed}" -ne 0 ]; then
    status=1
  fi
  exit "${status}"
}
trap 'cleanup "$?"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

bash "${ROOT}/production/scripts/init-secrets.sh" >/dev/null
compose_with_deadline 240 "production MySQL startup" \
  up --detach --wait mysql
compose_with_deadline 120 "production MySQL bootstrap" \
  run --rm --no-deps db-bootstrap

# Prove that rerunning the production bootstrap removes legacy role edges as
# well as direct grants. A fresh-volume-only test would miss this upgrade-path
# privilege escalation because MySQL's REVOKE ALL leaves roles intact.
compose_with_deadline 60 "production MySQL legacy-role injection" \
  exec --no-TTY mysql /bin/sh -ec '
  MYSQL_PWD="$(tr -d "\r\n" </run/secrets/mysql_root_password)"
  export MYSQL_PWD
  mysql --protocol=tcp --host=127.0.0.1 --user=root <<SQL
CREATE ROLE IF NOT EXISTS auris_legacy_gate_role;
GRANT SELECT ON mysql.* TO auris_legacy_gate_role;
GRANT auris_legacy_gate_role TO auris_runtime, auris_migration;
SQL
'
compose_with_deadline 60 "production MySQL privilege convergence" \
  run --rm --no-deps db-bootstrap

# Run the destructive cycle only against this script's isolated Compose project
# and named volume. The URL is read inside the container and never printed.
compose_with_deadline 900 "production MySQL full migration cycle" \
  run --rm --no-deps --build \
  --volume "${ROOT}/doc/backend-spec/seed-fixture-v0.1.json:/doc/backend-spec/seed-fixture-v0.1.json:ro" \
  --entrypoint /bin/sh migrate -ec '
  database_url="$(tr -d "\r\n" </run/secrets/migration_database_url)"
  unset DATABASE_URL_FILE
  exec python scripts/verify_migrations.py --database-url "${database_url}"
'

# Restore the isolated schema to the production head through the exact migrate
# service command that operators use.
compose_with_deadline 180 "production MySQL head migration" \
  run --rm --no-deps migrate

compose_with_deadline 120 "production MySQL migration security check" \
  run --rm --no-deps --entrypoint /bin/sh migrate -ec '
  exec python scripts/verify_mysql_migration_security.py \
    --database-url-file /run/secrets/migration_database_url \
    --expected-database auris_flow \
    --expected-user auris_migration \
    --expected-version-prefix 8.4. \
    --privilege-profile migration \
    --require-head-triggers
'

compose_with_deadline 120 "production MySQL runtime security check" \
  run --rm --no-deps --entrypoint /bin/sh bff -ec '
  exec python scripts/verify_mysql_migration_security.py \
    --database-url-file /run/secrets/runtime_database_url \
    --expected-database auris_flow \
    --expected-user auris_runtime \
    --expected-version-prefix 8.4. \
    --privilege-profile runtime \
    --require-runtime-trigger-probe
'

echo "production MySQL migration gate ok: isolated MySQL 8.4, exact grants/roles, full cycle"
