#!/bin/sh
set -eu

case "${AURIS_PUBLIC_HOST:-}" in
  ''|*[!A-Za-z0-9.-]*|.*|*.) echo "AURIS_PUBLIC_HOST is invalid" >&2; exit 2 ;;
esac

export KC_DB_PASSWORD="$(cat /run/secrets/keycloak_db_password)"
export KC_BOOTSTRAP_ADMIN_USERNAME="$(cat /run/secrets/keycloak_admin_user)"
export KC_BOOTSTRAP_ADMIN_PASSWORD="$(cat /run/secrets/keycloak_admin_password)"

bootstrap_password_file=/run/secrets/keycloak_bootstrap_operator_password
bootstrap_password="$(cat "${bootstrap_password_file}")"
case "${bootstrap_password}" in
  *[!0-9a-f]*|'') echo "bootstrap operator secret has an invalid format" >&2; exit 2 ;;
esac
if [ "${#bootstrap_password}" -ne 64 ]; then
  echo "bootstrap operator secret has an invalid length" >&2
  exit 2
fi
unset bootstrap_password

umask 077
mkdir -p /opt/keycloak/data/import
awk -v host="${AURIS_PUBLIC_HOST}" '
  BEGIN {
    password_file = "/run/secrets/keycloak_bootstrap_operator_password"
    if ((getline password < password_file) <= 0) {
      exit 2
    }
    close(password_file)
  }
  {
    gsub("__AURIS_PUBLIC_HOST__", host)
    gsub("__AURIS_BOOTSTRAP_OPERATOR_PASSWORD__", password)
    print
  }
' /opt/auris/auris-flow-realm.template.json \
  > /opt/keycloak/data/import/auris-flow-realm.json
chmod 600 /opt/keycloak/data/import/auris-flow-realm.json

exec /opt/keycloak/bin/kc.sh start --import-realm --http-port=8080
