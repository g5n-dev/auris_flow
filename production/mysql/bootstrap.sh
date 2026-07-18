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
GRANT SELECT, INSERT, UPDATE, DELETE ON auris_flow.* TO 'auris_runtime'@'%';
GRANT ALL PRIVILEGES ON auris_flow.* TO 'auris_migration'@'%';
GRANT ALL PRIVILEGES ON keycloak.* TO 'keycloak'@'%';
GRANT ALL PRIVILEGES ON dagster.* TO 'dagster'@'%';
FLUSH PRIVILEGES;
SQL
unset MYSQL_PWD
