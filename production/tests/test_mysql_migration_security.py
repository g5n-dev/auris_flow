from __future__ import annotations

import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "production" / "compose.yaml"
BOOTSTRAP = ROOT / "production" / "mysql" / "bootstrap.sh"
PRODUCTION_GATE = ROOT / "scripts" / "verify_production_mysql_migrations.sh"


def _granted_privileges(sql: str, *, schema: str, user: str) -> set[str]:
    match = re.search(
        rf"^GRANT[ \t]+([^;]+?)[ \t\n]+ON[ \t]+{re.escape(schema)}\.\*[ \t]+TO[ \t]+'{re.escape(user)}'@'%';",
        sql,
        flags=re.DOTALL | re.MULTILINE,
    )
    assert match is not None, f"missing grant for {user} on {schema}"
    return {item.strip().upper() for item in match.group(1).split(",")}


def test_production_mysql_allows_versioned_triggers_without_super_grant() -> None:
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    mysql = compose["services"]["mysql"]
    command = mysql["command"]

    assert "--log-bin-trust-function-creators=1" in command
    assert "--skip-log-bin" not in command
    assert "--disable-log-bin" not in command
    assert mysql["cap_drop"] == ["ALL"]
    assert set(mysql["cap_add"]) == {"CHOWN", "DAC_OVERRIDE", "SETGID", "SETUID"}

    gate = PRODUCTION_GATE.read_text(encoding="utf-8")
    assert "verify_mysql_migration_security.py" in gate
    assert "--require-head-triggers" in gate
    assert "--expected-user auris_migration" in gate
    assert "--expected-user auris_runtime" in gate
    assert "--privilege-profile runtime" in gate
    assert "--require-runtime-trigger-probe" in gate
    assert "production MySQL legacy-role injection" in gate
    assert "CREATE ROLE IF NOT EXISTS auris_legacy_gate_role" in gate
    assert "production MySQL privilege convergence" in gate
    assert "production MySQL gate diagnostics" in gate
    assert "logs --no-color mysql db-bootstrap" in gate
    assert "up --detach --wait mysql db-bootstrap" not in gate
    assert "run --rm --no-deps db-bootstrap" in gate
    assert (
        "${ROOT}/doc/backend-spec/seed-fixture-v0.1.json:"
        "/doc/backend-spec/seed-fixture-v0.1.json:ro"
    ) in gate
    assert "unset DATABASE_URL_FILE" in gate
    assert 'if ! compose_with_deadline 60 "production MySQL gate teardown"' in gate
    assert 'if [ "${status}" -eq 0 ] && [ "${cleanup_failed}" -ne 0 ]' in gate


def test_mysql_bootstrap_revokes_legacy_all_and_grants_exact_capabilities() -> None:
    bootstrap = BOOTSTRAP.read_text(encoding="utf-8")
    migration_privileges = {
        "SELECT",
        "INSERT",
        "UPDATE",
        "DELETE",
        "CREATE",
        "ALTER",
        "DROP",
        "INDEX",
        "REFERENCES",
        "TRIGGER",
    }
    assert "GRANT ALL PRIVILEGES ON auris_flow.* TO 'auris_migration'" not in bootstrap
    for user in ("auris_runtime", "auris_migration"):
        assert f"REVOKE ALL PRIVILEGES, GRANT OPTION FROM '{user}'@'%';" in bootstrap

    # REVOKE ALL does not remove granted roles in MySQL. The idempotent
    # bootstrap must also converge legacy role memberships before applying the
    # exact schema-level allowlist, otherwise a restarted production install can
    # retain privileges that SHOW GRANTS correctly rejects later.
    assert "FROM mysql.role_edges" in bootstrap
    assert "revoke_role_edges auris_runtime" in bootstrap
    assert "revoke_role_edges auris_migration" in bootstrap

    assert _granted_privileges(
        bootstrap, schema="auris_flow", user="auris_runtime"
    ) == {"SELECT", "INSERT", "UPDATE", "DELETE"}
    assert (
        _granted_privileges(bootstrap, schema="auris_flow", user="auris_migration")
        == migration_privileges
    )
