#!/bin/sh
set -eu

read_secret() {
  value="$(cat "/run/secrets/$1")"
  case "$value" in
    *[!A-Za-z0-9._~-]*|'') echo "invalid secret format: $1" >&2; exit 2 ;;
  esac
  printf '%s' "$value"
}

root_password="$(read_secret mysql_root_password)"
runtime_password="$(read_secret mysql_runtime_password)"
migration_password="$(read_secret mysql_migration_password)"
keycloak_password="$(read_secret keycloak_db_password)"
dagster_password="$(read_secret dagster_db_password)"

export MYSQL_PWD="$root_password"
mysql --protocol=tcp --host="${MYSQL_HOST:-mysql}" --user=root <<SQL
CREATE DATABASE IF NOT EXISTS auris_flow CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS keycloak CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS dagster CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'auris_runtime'@'%' IDENTIFIED BY '${runtime_password}';
CREATE USER IF NOT EXISTS 'auris_migration'@'%' IDENTIFIED BY '${migration_password}';
CREATE USER IF NOT EXISTS 'keycloak'@'%' IDENTIFIED BY '${keycloak_password}';
CREATE USER IF NOT EXISTS 'dagster'@'%' IDENTIFIED BY '${dagster_password}';
ALTER USER 'auris_runtime'@'%' IDENTIFIED BY '${runtime_password}';
ALTER USER 'auris_migration'@'%' IDENTIFIED BY '${migration_password}';
ALTER USER 'keycloak'@'%' IDENTIFIED BY '${keycloak_password}';
ALTER USER 'dagster'@'%' IDENTIFIED BY '${dagster_password}';
REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'auris_runtime'@'%';
REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'auris_migration'@'%';
SQL

# MySQL's REVOKE ALL PRIVILEGES does not revoke role memberships. Remove any
# legacy direct role edges as well so rerunning bootstrap converges existing
# installations to the same least-privilege account state as a fresh install.
revoke_role_edges() {
  account_user="$1"
  role_revokes="$(
    mysql --protocol=tcp --host="${MYSQL_HOST:-mysql}" --user=root \
      --batch --skip-column-names <<SQL
SELECT CONCAT(
  'REVOKE ', QUOTE(FROM_USER), '@', QUOTE(FROM_HOST),
  ' FROM ', QUOTE(TO_USER), '@', QUOTE(TO_HOST), ';'
)
FROM mysql.role_edges
WHERE TO_USER = '${account_user}' AND TO_HOST = '%';
SQL
  )"
  if [ -n "${role_revokes}" ]; then
    printf '%s\n' "${role_revokes}" |
      mysql --protocol=tcp --host="${MYSQL_HOST:-mysql}" --user=root
  fi
}

revoke_role_edges auris_runtime
revoke_role_edges auris_migration

mysql --protocol=tcp --host="${MYSQL_HOST:-mysql}" --user=root <<SQL
GRANT SELECT, INSERT, UPDATE, DELETE ON auris_flow.* TO 'auris_runtime'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, DROP, INDEX, REFERENCES,
  TRIGGER
  ON auris_flow.* TO 'auris_migration'@'%';
GRANT ALL PRIVILEGES ON keycloak.* TO 'keycloak'@'%';
GRANT ALL PRIVILEGES ON dagster.* TO 'dagster'@'%';
FLUSH PRIVILEGES;
SQL
unset MYSQL_PWD
