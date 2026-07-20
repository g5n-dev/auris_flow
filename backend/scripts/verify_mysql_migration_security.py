#!/usr/bin/env python3
"""Verify the production MySQL migration identity and trigger boundary."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections.abc import Sequence
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

MIGRATION_PRIVILEGES = frozenset(
    {
        "ALTER",
        "CREATE",
        "DELETE",
        "DROP",
        "INDEX",
        "INSERT",
        "REFERENCES",
        "SELECT",
        "TRIGGER",
        "UPDATE",
    }
)
RUNTIME_PRIVILEGES = frozenset({"DELETE", "INSERT", "SELECT", "UPDATE"})
PRIVILEGE_PROFILES = {
    "migration": MIGRATION_PRIVILEGES,
    "runtime": RUNTIME_PRIVILEGES,
}
EXPECTED_MYSQL_TRIGGERS = frozenset(
    {
        "trg_asr_annotation_corrections_no_delete",
        "trg_asr_annotation_corrections_no_update",
        "trg_calibration_adjudications_no_delete",
        "trg_calibration_adjudications_no_update",
        "trg_calibration_submissions_no_delete",
        "trg_calibration_submissions_no_update",
        "trg_experiment_assignments_no_delete",
        "trg_experiment_assignments_no_update",
        "trg_experiment_decisions_no_delete",
        "trg_experiment_decisions_no_update",
        "trg_experiment_exposures_no_delete",
        "trg_experiment_exposures_no_update",
        "trg_experiment_metric_snapshots_no_delete",
        "trg_experiment_metric_snapshots_no_update",
        "trg_experiment_outcomes_no_delete",
        "trg_experiment_outcomes_no_update",
        "trg_gold_annotations_no_delete",
        "trg_gold_annotations_no_update",
        "trg_gold_set_versions_no_delete",
        "trg_gold_set_versions_no_update",
        "trg_insight_report_metric_bindings_no_delete",
        "trg_insight_report_metric_bindings_no_update",
        "trg_label_calibration_versions_no_delete",
        "trg_label_calibration_versions_no_update",
        "trg_label_eval_results_no_delete",
        "trg_label_eval_results_no_update",
        "trg_label_eval_suite_results_no_delete",
        "trg_label_eval_suite_results_no_update",
        "trg_label_fact_set_head_events_no_delete",
        "trg_label_fact_set_head_events_no_update",
        "trg_label_facts_contract_insert",
        "trg_label_facts_no_delete",
        "trg_label_facts_no_update",
        "trg_label_mapping_bundle_members_no_delete",
        "trg_label_mapping_bundle_members_no_published_insert",
        "trg_label_mapping_bundle_members_no_update",
        "trg_label_mapping_bundle_paths_no_delete",
        "trg_label_mapping_bundle_paths_no_published_insert",
        "trg_label_mapping_bundle_paths_no_update",
        "trg_label_mapping_bundle_sources_no_delete",
        "trg_label_mapping_bundle_sources_no_published_insert",
        "trg_label_mapping_bundle_sources_no_update",
        "trg_label_mapping_bundles_published_delete",
        "trg_label_mapping_bundles_published_update",
        "trg_label_mapping_item_targets_no_delete",
        "trg_label_mapping_item_targets_no_published_insert",
        "trg_label_mapping_item_targets_no_retire",
        "trg_label_mapping_item_targets_no_update",
        "trg_label_mapping_items_no_delete",
        "trg_label_mapping_items_no_published_insert",
        "trg_label_mapping_items_no_update",
        "trg_label_mapping_versions_published_delete",
        "trg_label_mapping_versions_published_update",
        "trg_label_observations_no_delete",
        "trg_label_observations_no_update",
        "trg_label_optimization_metric_snapshots_no_delete",
        "trg_label_optimization_metric_snapshots_no_update",
        "trg_metric_result_label_scopes_no_delete",
        "trg_metric_result_label_scopes_no_update",
        "trg_metric_results_no_delete",
        "trg_metric_results_no_update",
        "trg_release_bundle_head_events_interval_insert",
        "trg_release_bundle_head_events_interval_update",
        "trg_release_bundle_head_events_no_delete",
        "trg_release_deployments_single_completed_insert",
        "trg_release_deployments_single_completed_update",
    }
)
EXPECTED_MYSQL_TRIGGER_MANIFEST_SHA256 = (
    "bdbb92d0a6219fed0992882ef92ac56059804d5c9c70e55e65ac8cd246c1b8de"
)

_IDENTIFIER = r"(?:`(?:``|[^`])+`|'(?:''|[^'])+')"
_GRANT_RE = re.compile(
    rf"^GRANT (?P<privileges>[A-Z_, ]+) ON (?P<schema>\*|{_IDENTIFIER})\.\* "
    rf"TO (?P<user>{_IDENTIFIER})@(?P<host>{_IDENTIFIER})$",
    flags=re.IGNORECASE,
)
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _unquote_identifier(value: str) -> str:
    if value.startswith("`"):
        return value[1:-1].replace("``", "`")
    return value[1:-1].replace("''", "'")


def parse_grant(grant: str) -> tuple[str, frozenset[str], str, str]:
    match = _GRANT_RE.fullmatch(grant.strip())
    if match is None:
        raise ValueError(f"unsupported SHOW GRANTS statement: {grant!r}")
    schema_token = match.group("schema")
    schema = "*" if schema_token == "*" else _unquote_identifier(schema_token)
    privileges = frozenset(
        privilege.strip().upper() for privilege in match.group("privileges").split(",")
    )
    return (
        f"{schema}.*",
        privileges,
        _unquote_identifier(match.group("user")),
        _unquote_identifier(match.group("host")),
    )


def expected_grants(
    *,
    profile: str,
    database: str,
    user: str,
) -> set[tuple[str, frozenset[str], str, str]]:
    try:
        privileges = PRIVILEGE_PROFILES[profile]
    except KeyError:
        raise ValueError(f"unsupported privilege profile: {profile}") from None
    return {
        ("*.*", frozenset({"USAGE"}), user, "%"),
        (f"{database}.*", privileges, user, "%"),
    }


def expected_migration_heads() -> frozenset[str]:
    backend_root = Path(__file__).resolve().parents[1]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "migrations"))
    return frozenset(ScriptDirectory.from_config(config).get_heads())


def expected_trigger_metadata(trigger_name: str) -> tuple[str, str, str]:
    if trigger_name == "trg_label_facts_contract_insert":
        return ("label_facts", "INSERT", "BEFORE")
    if trigger_name == "trg_label_mapping_item_targets_no_retire":
        return ("label_mapping_item_targets", "INSERT", "BEFORE")
    if trigger_name.startswith("trg_release_bundle_head_events_interval_"):
        event = trigger_name.rsplit("_", 1)[1].upper()
        return ("release_bundle_head_events", event, "BEFORE")
    if trigger_name.startswith("trg_release_deployments_single_completed_"):
        event = trigger_name.rsplit("_", 1)[1].upper()
        return ("release_deployments", event, "BEFORE")
    if trigger_name.endswith("_no_published_insert"):
        table = trigger_name.removeprefix("trg_").removesuffix("_no_published_insert")
        return (table, "INSERT", "BEFORE")
    published_match = re.fullmatch(r"trg_(.+)_published_(update|delete)", trigger_name)
    if published_match is not None:
        return (published_match.group(1), published_match.group(2).upper(), "BEFORE")
    append_only_match = re.fullmatch(r"trg_(.+)_no_(update|delete)", trigger_name)
    if append_only_match is not None:
        return (append_only_match.group(1), append_only_match.group(2).upper(), "BEFORE")
    raise ValueError(f"trigger has no expected metadata rule: {trigger_name}")


def trigger_manifest_sha256(rows: Sequence[Sequence[object]]) -> str:
    manifest = [
        [
            str(row[0]),
            str(row[1]),
            str(row[2]).upper(),
            str(row[3]).upper(),
            re.sub(r"\s+", " ", str(row[4]).strip()),
        ]
        for row in rows
    ]
    canonical = json.dumps(manifest, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _load_database_url(args: argparse.Namespace) -> str:
    if args.database_url_file is not None:
        value = args.database_url_file.read_text(encoding="utf-8").strip()
    else:
        value = args.database_url or os.environ.get("DATABASE_URL", "").strip()
    if not value:
        raise SystemExit("DATABASE_URL or --database-url-file is required")
    return value


def _assert_safe_identifier(value: str, label: str) -> None:
    if _SAFE_IDENTIFIER_RE.fullmatch(value) is None:
        raise SystemExit(f"{label} must contain only ASCII letters, digits, or underscore")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url")
    parser.add_argument("--database-url-file", type=Path)
    parser.add_argument("--expected-database", required=True)
    parser.add_argument("--expected-user", required=True)
    parser.add_argument("--expected-version-prefix", default="8.4.")
    parser.add_argument("--database-timeout-seconds", type=int, default=15)
    parser.add_argument(
        "--privilege-profile",
        choices=tuple(PRIVILEGE_PROFILES),
        default="migration",
    )
    parser.add_argument("--require-head-triggers", action="store_true")
    parser.add_argument("--require-runtime-trigger-probe", action="store_true")
    args = parser.parse_args(argv)
    if args.database_url and args.database_url_file:
        parser.error("--database-url and --database-url-file are mutually exclusive")
    _assert_safe_identifier(args.expected_database, "--expected-database")
    _assert_safe_identifier(args.expected_user, "--expected-user")
    if args.require_head_triggers and args.privilege_profile != "migration":
        parser.error("--require-head-triggers requires --privilege-profile migration")
    if args.require_runtime_trigger_probe and args.privilege_profile != "runtime":
        parser.error("--require-runtime-trigger-probe requires --privilege-profile runtime")
    if args.database_timeout_seconds <= 0:
        parser.error("--database-timeout-seconds must be positive")
    return args


def _assert_cross_database_denied(connection: Connection) -> None:
    try:
        connection.execute(text("SELECT COUNT(*) FROM mysql.user"))
    except DBAPIError as exc:
        error_args = getattr(exc.orig, "args", ())
        if not error_args or error_args[0] not in {1142, 1143}:
            raise
        connection.rollback()
        return
    raise SystemExit("database identity unexpectedly reads mysql.user")


def _assert_runtime_trigger_execution(connection: Connection) -> None:
    statement = text(
        "INSERT INTO label_facts ("
        "fact_id, tenant_id, project_id, aggregate_id, supersedes_fact_id, "
        "fact_namespace, logical_key_sha, revision, event_or_segment_id, assertion_slot, "
        "occurred_at, recorded_at, occurred_at_origin, source_kind, "
        "human_review_decision_id, recompute_run_item_id, fact_set_id, content_sha256, "
        "root_trace_id, action_trace_id, label_version_id, subject_scope, subject_key, "
        "label_id, value_type, value_json, authority, status, active_slot, "
        "review_decision_id, trace_id, payload"
        ") VALUES ("
        "'fact_runtime_security_probe', 'migration_tenant', 'migration_project', "
        "'aggregate_runtime_security_probe', NULL, 'production', :logical_key_sha, 1, "
        "'segment-runtime-security-probe', 'assertion-main', "
        "'2026-07-20 00:00:00.000000', '2026-07-20 00:00:01.000000', "
        "'source', 'aggregate', NULL, NULL, NULL, :content_sha256, "
        "'trace_runtime_security_root', 'trace_runtime_security_action', "
        "'lv_runtime_security_probe', 'audio-segment', 'segment-runtime-security-probe', "
        "'label-runtime-security-probe', 'boolean', 'true', 'model-consensus', "
        "'active', 'active', NULL, 'trace_runtime_security_probe', :payload"
        ")"
    )
    try:
        connection.execute(
            statement,
            {
                "logical_key_sha": "7" * 64,
                "content_sha256": "8" * 64,
                "payload": '{"migration_security_probe":true}',
            },
        )
    except DBAPIError as exc:
        error_args = getattr(exc.orig, "args", ())
        if (
            not error_args
            or error_args[0] != 1644
            or str(error_args[1]) != "label_facts contract requires recorded rows"
        ):
            raise
        connection.rollback()
    else:
        raise SystemExit("runtime identity bypassed the LabelFact insert trigger")

    try:
        connection.execute(text("DROP TRIGGER trg_label_facts_no_update"))
    except DBAPIError as exc:
        error_args = getattr(exc.orig, "args", ())
        if not error_args or error_args[0] not in {1142, 1227}:
            raise
        connection.rollback()
    else:
        raise SystemExit("runtime identity unexpectedly dropped a LabelFact trigger")


def verify(args: argparse.Namespace) -> None:
    database_url = _load_database_url(args)
    engine = create_engine(
        database_url,
        pool_pre_ping=True,
        connect_args={
            "connect_timeout": args.database_timeout_seconds,
            "read_timeout": args.database_timeout_seconds,
            "write_timeout": args.database_timeout_seconds,
        },
    )
    try:
        if engine.dialect.name not in {"mysql", "mariadb"}:
            raise SystemExit(f"expected MySQL, got {engine.dialect.name}")
        with engine.connect() as connection:
            current_user, current_database, version, log_bin, trust = connection.execute(
                text(
                    "SELECT CURRENT_USER(), DATABASE(), VERSION(), "
                    "@@GLOBAL.log_bin, @@GLOBAL.log_bin_trust_function_creators"
                )
            ).one()
            expected_account = f"{args.expected_user}@%"
            if str(current_user) != expected_account:
                raise SystemExit(
                    f"unexpected migration identity: {current_user!s}; expected {expected_account}"
                )
            if str(current_database) != args.expected_database:
                raise SystemExit(
                    f"unexpected migration database: {current_database!s}; "
                    f"expected {args.expected_database}"
                )
            if not str(version).startswith(args.expected_version_prefix):
                raise SystemExit(
                    f"unexpected MySQL version {version!s}; "
                    f"expected prefix {args.expected_version_prefix}"
                )
            if int(log_bin) != 1:
                raise SystemExit("production MySQL binary logging is not enabled")
            if int(trust) != 1:
                raise SystemExit("production MySQL trigger creator policy is not enabled")

            parsed_grants = {
                parse_grant(str(row[0]))
                for row in connection.execute(text("SHOW GRANTS FOR CURRENT_USER()"))
            }
            grant_allowlist = expected_grants(
                profile=args.privilege_profile,
                database=args.expected_database,
                user=args.expected_user,
            )
            if parsed_grants != grant_allowlist:
                raise SystemExit(
                    f"{args.privilege_profile} SHOW GRANTS differs from the exact "
                    "production allowlist: "
                    f"{sorted(parsed_grants)!r}"
                )
            _assert_cross_database_denied(connection)

            if args.require_runtime_trigger_probe:
                _assert_runtime_trigger_execution(connection)

            if args.require_head_triggers:
                database_heads = frozenset(
                    str(row[0])
                    for row in connection.execute(text("SELECT version_num FROM alembic_version"))
                )
                repository_heads = expected_migration_heads()
                if database_heads != repository_heads:
                    raise SystemExit(
                        "database migration head differs from repository head: "
                        f"database={sorted(database_heads)!r}, "
                        f"repository={sorted(repository_heads)!r}"
                    )
                trigger_rows = connection.execute(
                    text(
                        "SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE, EVENT_MANIPULATION, "
                        "ACTION_TIMING, ACTION_STATEMENT, DEFINER "
                        "FROM information_schema.TRIGGERS "
                        "WHERE TRIGGER_SCHEMA = :schema ORDER BY TRIGGER_NAME"
                    ),
                    {"schema": args.expected_database},
                ).all()
                actual_triggers = frozenset(str(row[0]) for row in trigger_rows)
                if actual_triggers != EXPECTED_MYSQL_TRIGGERS:
                    missing = sorted(EXPECTED_MYSQL_TRIGGERS - actual_triggers)
                    unexpected = sorted(actual_triggers - EXPECTED_MYSQL_TRIGGERS)
                    raise SystemExit(
                        "MySQL trigger manifest differs from the expected head: "
                        f"missing={missing!r}, unexpected={unexpected!r}"
                    )
                actual_metadata = {
                    str(row[0]): (str(row[1]), str(row[2]).upper(), str(row[3]).upper())
                    for row in trigger_rows
                }
                expected_metadata = {
                    trigger_name: expected_trigger_metadata(trigger_name)
                    for trigger_name in EXPECTED_MYSQL_TRIGGERS
                }
                if actual_metadata != expected_metadata:
                    invalid_metadata = sorted(
                        f"{name}: actual={actual_metadata.get(name)!r}, "
                        f"expected={expected_metadata[name]!r}"
                        for name in EXPECTED_MYSQL_TRIGGERS
                        if actual_metadata.get(name) != expected_metadata[name]
                    )
                    raise SystemExit(
                        "MySQL triggers have incorrect table/event/timing metadata: "
                        f"{invalid_metadata!r}"
                    )
                actual_manifest_sha256 = trigger_manifest_sha256(trigger_rows)
                if actual_manifest_sha256 != EXPECTED_MYSQL_TRIGGER_MANIFEST_SHA256:
                    raise SystemExit(
                        "MySQL trigger definition manifest differs from the controlled head: "
                        f"actual_sha256={actual_manifest_sha256}, "
                        f"expected_sha256={EXPECTED_MYSQL_TRIGGER_MANIFEST_SHA256}"
                    )
                invalid_definers = sorted(
                    f"{row[0]}={row[5]}" for row in trigger_rows if str(row[5]) != expected_account
                )
                if invalid_definers:
                    raise SystemExit(
                        f"MySQL triggers have uncontrolled DEFINER identities: {invalid_definers!r}"
                    )
    finally:
        engine.dispose()

    detail = f"{args.privilege_profile} grant boundary"
    if args.require_head_triggers:
        detail = f"head and {len(EXPECTED_MYSQL_TRIGGERS)} controlled triggers"
    elif args.require_runtime_trigger_probe:
        detail = "runtime grant boundary and append-only trigger execution"
    print(
        "MySQL migration security gate ok: "
        f"{args.expected_user}@%, log_bin=1, exact grants, {detail}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    verify(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
