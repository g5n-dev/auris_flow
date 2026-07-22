#!/bin/sh
set -eu

webhook_file=/run/secrets/alertmanager_webhook_url

fail_secret() {
  echo "Alertmanager webhook secret is missing or invalid." >&2
  exit 2
}

if [ ! -f "${webhook_file}" ] || [ ! -s "${webhook_file}" ] || [ ! -r "${webhook_file}" ]; then
  fail_secret
fi

webhook_size="$(wc -c < "${webhook_file}" | tr -d '[:space:]')"
if [ -z "${webhook_size}" ] || [ "${webhook_size}" -gt 4096 ]; then
  fail_secret
fi

# Accept exactly one non-whitespace HTTPS URL. Never copy its value into an
# argument, environment variable, diagnostic, or process listing.
if ! grep -Eq '^https://[^[:space:]]+$' "${webhook_file}"; then
  fail_secret
fi
if [ "$(grep -Ec '^https://[^[:space:]]+$' "${webhook_file}")" -ne 1 ] || \
  grep -Evq '^https://[^[:space:]]+$' "${webhook_file}"; then
  fail_secret
fi

exec /bin/alertmanager "$@"
