from __future__ import annotations

import pytest

from scripts.verify_migrations import resolve_layout
from scripts.verify_mysql_migration_security import (
    MIGRATION_PRIVILEGES,
    RUNTIME_PRIVILEGES,
    expected_grants,
    expected_trigger_metadata,
    parse_args,
    parse_grant,
    trigger_manifest_sha256,
)


def test_parse_grant_normalizes_the_exact_migration_allowlist() -> None:
    grant = (
        "GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, REFERENCES, INDEX, "
        "ALTER, TRIGGER ON `auris_flow`.* TO `auris_migration`@`%`"
    )

    assert parse_grant(grant) == (
        "auris_flow.*",
        MIGRATION_PRIVILEGES,
        "auris_migration",
        "%",
    )
    assert parse_grant("GRANT USAGE ON *.* TO 'auris_migration'@'%'") == (
        "*.*",
        frozenset({"USAGE"}),
        "auris_migration",
        "%",
    )


@pytest.mark.parametrize(
    "grant",
    [
        "REVOKE GRANT OPTION ON *.* FROM `auris_migration`@`%`; GRANT SUPER ON *.* "
        "TO `auris_migration`@`%`",
        "GRANT SELECT ON `auris_flow`.* TO `auris_migration`@`%` WITH GRANT OPTION",
        "GRANT SELECT ON `mysql`.* TO `auris_migration`@`%`;",
    ],
)
def test_parse_grant_rejects_cross_statement_or_extended_syntax(grant: str) -> None:
    with pytest.raises(ValueError, match="unsupported SHOW GRANTS statement"):
        parse_grant(grant)


def test_expected_grants_are_exact_for_migration_and_runtime_identities() -> None:
    assert expected_grants(
        profile="migration",
        database="auris_flow",
        user="auris_migration",
    ) == {
        ("*.*", frozenset({"USAGE"}), "auris_migration", "%"),
        ("auris_flow.*", MIGRATION_PRIVILEGES, "auris_migration", "%"),
    }
    assert expected_grants(
        profile="runtime",
        database="auris_flow",
        user="auris_runtime",
    ) == {
        ("*.*", frozenset({"USAGE"}), "auris_runtime", "%"),
        ("auris_flow.*", RUNTIME_PRIVILEGES, "auris_runtime", "%"),
    }


def test_runtime_trigger_probe_can_only_run_with_runtime_profile() -> None:
    common = [
        "--database-url",
        "mysql+pymysql://user:password@127.0.0.1:3306/auris_flow",
        "--expected-database",
        "auris_flow",
        "--expected-user",
        "auris_runtime",
        "--require-runtime-trigger-probe",
    ]
    with pytest.raises(SystemExit):
        parse_args(common)

    args = parse_args([*common, "--privilege-profile", "runtime"])
    assert args.require_runtime_trigger_probe is True


def test_migration_verifier_resolves_repo_and_production_image_layouts(
    tmp_path,
) -> None:
    repo_script = tmp_path / "checkout" / "backend" / "scripts" / "verify_migrations.py"
    image_script = tmp_path / "image" / "app" / "scripts" / "verify_migrations.py"

    assert resolve_layout(repo_script) == (
        tmp_path / "checkout",
        tmp_path / "checkout" / "backend",
    )
    assert resolve_layout(image_script) == (
        tmp_path / "image",
        tmp_path / "image" / "app",
    )


def test_trigger_metadata_covers_regular_and_exception_names() -> None:
    assert expected_trigger_metadata("trg_label_facts_no_update") == (
        "label_facts",
        "UPDATE",
        "BEFORE",
    )
    assert expected_trigger_metadata("trg_label_mapping_item_targets_no_retire") == (
        "label_mapping_item_targets",
        "INSERT",
        "BEFORE",
    )
    assert expected_trigger_metadata("trg_release_bundle_head_events_interval_insert") == (
        "release_bundle_head_events",
        "INSERT",
        "BEFORE",
    )


def test_trigger_manifest_digest_normalizes_only_whitespace() -> None:
    base = [["trigger", "table", "INSERT", "BEFORE", "BEGIN  SIGNAL X; END", "user@%"]]
    spaced = [["trigger", "table", "insert", "before", "BEGIN\n SIGNAL X;\tEND", "other@%"]]

    assert trigger_manifest_sha256(base) == trigger_manifest_sha256(spaced)
