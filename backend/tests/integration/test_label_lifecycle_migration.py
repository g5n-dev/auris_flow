from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REVISION_BEFORE = "0033_task_version_release_heads"
REVISION_EXPAND = "0034_label_lifecycle_mapping_expand"

EXPAND_TABLES = {
    "label_taxonomies",
    "label_mapping_versions",
    "label_mapping_items",
    "label_mapping_item_targets",
    "label_mapping_bundles",
    "label_mapping_bundle_sources",
    "label_mapping_bundle_members",
    "label_mapping_bundle_paths",
    "release_bundle_head_events",
}

LABEL_VERSION_EXPAND_COLUMNS = {
    "taxonomy_id",
    "semantic_version",
    "base_label_version_id",
    "artifact_status",
    "artifact_published_at",
    "artifact_deprecated_at",
    "deprecation_reason",
    "replacement_label_version_id",
    "content_sha256",
}


def _alembic(database_url: str, *arguments: str) -> None:
    environment = {**os.environ, "DATABASE_URL": database_url}
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", *arguments],
        cwd=BACKEND_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )


def _sqlite_engine(database_url: str) -> Engine:
    engine = create_engine(database_url, future=True)

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(connection: object, _record: object) -> None:
        if isinstance(connection, sqlite3.Connection):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def _revision(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(connection.scalar(text("SELECT version_num FROM alembic_version")))


def _insert_label_version(
    connection: object,
    *,
    tenant_id: str,
    project_id: str,
    label_version_id: str,
    status: str = "draft",
) -> None:
    connection.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO label_versions "
            "(label_version_id, tenant_id, project_id, status, resource_version, "
            "trace_id, payload) VALUES "
            "(:label_version_id, :tenant_id, :project_id, :status, 1, :trace_id, :payload)"
        ),
        {
            "label_version_id": label_version_id,
            "tenant_id": tenant_id,
            "project_id": project_id,
            "status": status,
            "trace_id": f"trace_{label_version_id}",
            "payload": json.dumps({"legacy": True}),
        },
    )


def _insert_label_item(
    connection: object,
    *,
    tenant_id: str,
    project_id: str,
    label_version_id: str,
    label_id: str,
) -> None:
    connection.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO label_version_items "
            "(label_version_item_id, tenant_id, project_id, label_version_id, label_id, "
            "canonical_name, aliases, value_type, risk_level, parent_ids, aggregation_rule, "
            "status, trace_id) VALUES "
            "(:item_id, :tenant_id, :project_id, :label_version_id, :label_id, :label_id, "
            ":empty_list, 'boolean', 'low', :empty_list, :empty_object, 'active', :trace_id)"
        ),
        {
            "item_id": f"lvi_{label_version_id}_{label_id}",
            "tenant_id": tenant_id,
            "project_id": project_id,
            "label_version_id": label_version_id,
            "label_id": label_id,
            "empty_list": json.dumps([]),
            "empty_object": json.dumps({}),
            "trace_id": f"trace_{label_version_id}_{label_id}",
        },
    )


def _insert_mapping_fixture(engine: Engine) -> None:
    with engine.begin() as connection:
        _insert_label_version(
            connection,
            tenant_id="tenant_a",
            project_id="project_a",
            label_version_id="lv_source_a",
            status="published",
        )
        _insert_label_version(
            connection,
            tenant_id="tenant_a",
            project_id="project_a",
            label_version_id="lv_target_a",
            status="published",
        )
        _insert_label_version(
            connection,
            tenant_id="tenant_b",
            project_id="project_b",
            label_version_id="lv_target_b",
            status="published",
        )
        _insert_label_item(
            connection,
            tenant_id="tenant_a",
            project_id="project_a",
            label_version_id="lv_source_a",
            label_id="label_old",
        )
        _insert_label_item(
            connection,
            tenant_id="tenant_a",
            project_id="project_a",
            label_version_id="lv_source_a",
            label_id="label_late",
        )
        _insert_label_item(
            connection,
            tenant_id="tenant_a",
            project_id="project_a",
            label_version_id="lv_target_a",
            label_id="label_new",
        )
        _insert_label_item(
            connection,
            tenant_id="tenant_b",
            project_id="project_b",
            label_version_id="lv_target_b",
            label_id="label_foreign",
        )


def _insert_published_mapping(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO label_mapping_versions "
                "(mapping_version_id, tenant_id, project_id, source_label_version_id, "
                "target_label_version_id, mapping_version, status, source_resource_version, "
                "target_resource_version, resource_version, content_sha256, approved_by, "
                "approved_at, published_at, root_trace_id, trace_id, payload) VALUES "
                "('lmv_a', 'tenant_a', 'project_a', 'lv_source_a', 'lv_target_a', '1.0.0', "
                "'approved', 1, 1, 1, :sha, 'user_reviewer', CURRENT_TIMESTAMP, "
                "CURRENT_TIMESTAMP, 'trace_root_mapping', 'trace_mapping', :payload)"
            ),
            {"sha": "a" * 64, "payload": json.dumps({"approved": True})},
        )
        connection.execute(
            text(
                "INSERT INTO label_mapping_items "
                "(mapping_item_id, tenant_id, project_id, mapping_version_id, "
                "source_label_version_id, target_label_version_id, source_label_id, "
                "relation, compatibility, comparability_status, allowed_metric_families, "
                "metric_grain, lineage_key, reducer, requires_recompute, "
                "source_semantic_sha256, target_semantic_sha256, content_sha256, trace_id, "
                "payload) VALUES "
                "('lmi_a', 'tenant_a', 'project_a', 'lmv_a', 'lv_source_a', 'lv_target_a', "
                "'label_old', 'replace', 'structural-break', "
                "'structural-break', :families, 'business-event', 'event_id', NULL, 0, "
                ":source_sha, :target_sha, :content_sha, 'trace_mapping_item', :payload)"
            ),
            {
                "families": json.dumps([]),
                "source_sha": "b" * 64,
                "target_sha": "c" * 64,
                "content_sha": "d" * 64,
                "payload": json.dumps({}),
            },
        )
        connection.execute(
            text(
                "INSERT INTO label_mapping_item_targets "
                "(mapping_item_target_id, tenant_id, project_id, mapping_version_id, "
                "mapping_item_id, target_label_version_id, target_label_id, target_order, "
                "content_sha256, trace_id, payload) VALUES "
                "('lmit_a', 'tenant_a', 'project_a', 'lmv_a', 'lmi_a', 'lv_target_a', "
                "'label_new', 0, :sha, 'trace_mapping_target', :payload)"
            ),
            {"sha": "3" * 64, "payload": json.dumps({})},
        )
        connection.execute(
            text(
                "INSERT INTO label_mapping_bundles "
                "(mapping_bundle_id, tenant_id, project_id, target_label_version_id, "
                "source_label_version_ids, source_manifest_sha256, compiler_version, status, "
                "resource_version, canonical_manifest_sha256, approved_by, approved_at, "
                "published_at, root_trace_id, trace_id, payload) VALUES "
                "('lmb_a', 'tenant_a', 'project_a', 'lv_target_a', :sources, :source_sha, "
                "'mapping-compiler/1', 'approved', 1, :manifest_sha, 'user_reviewer', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'trace_root_bundle', 'trace_bundle', "
                ":payload)"
            ),
            {
                "sources": json.dumps(["lv_source_a"]),
                "source_sha": "e" * 64,
                "manifest_sha": "f" * 64,
                "payload": json.dumps({}),
            },
        )
        connection.execute(
            text(
                "INSERT INTO label_mapping_bundle_sources "
                "(bundle_source_id, tenant_id, project_id, mapping_bundle_id, "
                "source_label_version_id, source_resource_version, source_order, "
                "content_sha256, trace_id, payload) VALUES "
                "('lmbs_a', 'tenant_a', 'project_a', 'lmb_a', 'lv_source_a', 1, 0, "
                ":sha, 'trace_bundle_source', :payload)"
            ),
            {"sha": "4" * 64, "payload": json.dumps({})},
        )
        connection.execute(
            text(
                "INSERT INTO label_mapping_bundle_members "
                "(bundle_member_id, tenant_id, project_id, mapping_bundle_id, "
                "mapping_version_id, source_label_version_id, target_label_version_id, "
                "edge_order, edge_content_sha256, trace_id, payload) VALUES "
                "('lmbm_a', 'tenant_a', 'project_a', 'lmb_a', 'lmv_a', 'lv_source_a', "
                "'lv_target_a', 0, :sha, 'trace_bundle_member', :payload)"
            ),
            {"sha": "a" * 64, "payload": json.dumps({})},
        )
        connection.execute(
            text(
                "INSERT INTO label_mapping_bundle_paths "
                "(bundle_path_id, tenant_id, project_id, mapping_bundle_id, "
                "source_label_version_id, target_label_version_id, source_label_id, "
                "target_label_id, metric_family, relation_path, mapping_version_ids, "
                "comparability_status, requires_recompute, path_sha256, trace_id, payload) "
                "VALUES ('lmbp_a', 'tenant_a', 'project_a', 'lmb_a', 'lv_source_a', "
                "'lv_target_a', 'label_old', 'label_new', 'presence', :relations, :versions, "
                "'structural-break', 0, :sha, 'trace_bundle_path', :payload)"
            ),
            {
                "relations": json.dumps(["replace"]),
                "versions": json.dumps(["lmv_a"]),
                "sha": "1" * 64,
                "payload": json.dumps({}),
            },
        )
        connection.execute(
            text(
                "INSERT INTO release_bundle_head_events "
                "(head_event_id, tenant_id, project_id, environment, generation, "
                "previous_generation, action, activation_status, old_label_version_id, "
                "new_label_version_id, effective_from, content_sha256, actor_id, "
                "root_trace_id, trace_id, payload) "
                "VALUES ('rbhe_a', 'tenant_a', 'project_a', 'production', 1, NULL, "
                "'bootstrap', 'active', NULL, 'lv_target_a', CURRENT_TIMESTAMP, :sha, "
                "'user_reviewer', 'trace_root_head', 'trace_head', :payload)"
            ),
            {"sha": "2" * 64, "payload": json.dumps({})},
        )
        connection.execute(
            text(
                "UPDATE label_mapping_versions SET status = 'published', resource_version = 2 "
                "WHERE mapping_version_id = 'lmv_a'"
            )
        )
        connection.execute(
            text(
                "UPDATE label_mapping_bundles SET status = 'published', resource_version = 2 "
                "WHERE mapping_bundle_id = 'lmb_a'"
            )
        )


def test_0034_expand_is_reversible_and_preserves_legacy_rows(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'label_lifecycle_expand.sqlite'}"
    _alembic(database_url, "upgrade", REVISION_BEFORE)
    engine = _sqlite_engine(database_url)
    try:
        with engine.begin() as connection:
            _insert_label_version(
                connection,
                tenant_id="tenant_legacy",
                project_id="project_legacy",
                label_version_id="lv_legacy",
                status="gray_releasing",
            )
            _insert_label_item(
                connection,
                tenant_id="tenant_legacy",
                project_id="project_legacy",
                label_version_id="lv_legacy",
                label_id="label_legacy",
            )
    finally:
        engine.dispose()

    _alembic(database_url, "upgrade", REVISION_EXPAND)
    engine = _sqlite_engine(database_url)
    try:
        assert _revision(engine) == REVISION_EXPAND
        inspector = inspect(engine)
        assert EXPAND_TABLES <= set(inspector.get_table_names())
        label_version_columns = {
            column["name"]: column for column in inspector.get_columns("label_versions")
        }
        assert LABEL_VERSION_EXPAND_COLUMNS <= set(label_version_columns)
        assert all(label_version_columns[name]["nullable"] for name in LABEL_VERSION_EXPAND_COLUMNS)
        item_columns = {
            column["name"]: column for column in inspector.get_columns("label_version_items")
        }
        assert item_columns["definition_sha256"]["nullable"] is True
        with engine.connect() as connection:
            legacy = (
                connection.execute(
                    text(
                        "SELECT status, artifact_status, taxonomy_id, semantic_version, payload "
                        "FROM label_versions WHERE label_version_id = 'lv_legacy'"
                    )
                )
                .mappings()
                .one()
            )
        assert legacy["status"] == "gray_releasing"
        assert legacy["artifact_status"] is None
        assert legacy["taxonomy_id"] is None
        assert legacy["semantic_version"] is None
        assert json.loads(legacy["payload"]) == {"legacy": True}
    finally:
        engine.dispose()

    _alembic(database_url, "downgrade", REVISION_BEFORE)
    engine = _sqlite_engine(database_url)
    try:
        assert _revision(engine) == REVISION_BEFORE
        inspector = inspect(engine)
        assert not (EXPAND_TABLES & set(inspector.get_table_names()))
        assert not (
            LABEL_VERSION_EXPAND_COLUMNS
            & {column["name"] for column in inspector.get_columns("label_versions")}
        )
        with engine.connect() as connection:
            legacy = (
                connection.execute(
                    text(
                        "SELECT status, payload FROM label_versions "
                        "WHERE label_version_id = 'lv_legacy'"
                    )
                )
                .mappings()
                .one()
            )
        assert legacy["status"] == "gray_releasing"
        assert json.loads(legacy["payload"]) == {"legacy": True}
    finally:
        engine.dispose()


def test_0034_rejects_cross_scope_mapping_references(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'label_lifecycle_scope.sqlite'}"
    _alembic(database_url, "upgrade", REVISION_EXPAND)
    engine = _sqlite_engine(database_url)
    try:
        _insert_mapping_fixture(engine)
        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO label_mapping_versions "
                        "(mapping_version_id, tenant_id, project_id, source_label_version_id, "
                        "target_label_version_id, mapping_version, status, "
                        "source_resource_version, target_resource_version, resource_version, "
                        "content_sha256, root_trace_id, trace_id, payload) VALUES "
                        "('lmv_cross', 'tenant_a', 'project_a', 'lv_source_a', 'lv_target_b', "
                        "'1.0.0', 'draft', 1, 1, 1, :sha, 'trace_root_cross', "
                        "'trace_cross', :payload)"
                    ),
                    {"sha": "9" * 64, "payload": json.dumps({})},
                )
    finally:
        engine.dispose()


def test_0034_enforces_mapping_shape_and_append_only_ledgers(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'label_lifecycle_guards.sqlite'}"
    _alembic(database_url, "upgrade", REVISION_EXPAND)
    engine = _sqlite_engine(database_url)
    try:
        _insert_mapping_fixture(engine)
        _insert_published_mapping(engine)

        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO label_mapping_versions "
                    "(mapping_version_id, tenant_id, project_id, source_label_version_id, "
                    "target_label_version_id, mapping_version, status, "
                    "source_resource_version, target_resource_version, resource_version, "
                    "content_sha256, root_trace_id, trace_id, payload) VALUES "
                    "('lmv_approved', 'tenant_a', 'project_a', 'lv_source_a', "
                    "'lv_target_a', '2.0.0', 'approved', 1, 1, 1, :sha, "
                    "'trace_root_approved', 'trace_approved', :payload)"
                ),
                {"sha": "5" * 64, "payload": json.dumps({})},
            )
            connection.execute(
                text(
                    "INSERT INTO label_mapping_bundles "
                    "(mapping_bundle_id, tenant_id, project_id, target_label_version_id, "
                    "source_label_version_ids, source_manifest_sha256, compiler_version, "
                    "status, resource_version, canonical_manifest_sha256, root_trace_id, "
                    "trace_id, payload) VALUES "
                    "('lmb_approved', 'tenant_a', 'project_a', 'lv_target_a', :sources, "
                    ":source_sha, 'mapping-compiler/1', 'approved', 1, :manifest_sha, "
                    "'trace_root_bundle_approved', 'trace_bundle_approved', :payload)"
                ),
                {
                    "sources": json.dumps(["lv_source_a"]),
                    "source_sha": "6" * 64,
                    "manifest_sha": "7" * 64,
                    "payload": json.dumps({}),
                },
            )

        invalid_statements = (
            "UPDATE label_mapping_versions SET content_sha256 = 'changed' "
            "WHERE mapping_version_id = 'lmv_a'",
            "UPDATE label_mapping_versions SET status = 'draft' WHERE mapping_version_id = 'lmv_a'",
            "UPDATE label_mapping_versions SET status = 'published', "
            "content_sha256 = 'changed-at-publish' "
            "WHERE mapping_version_id = 'lmv_approved'",
            "UPDATE label_mapping_items SET reducer = 'sum' WHERE mapping_item_id = 'lmi_a'",
            "UPDATE label_mapping_item_targets SET target_order = 1 "
            "WHERE mapping_item_target_id = 'lmit_a'",
            "DELETE FROM label_mapping_bundles WHERE mapping_bundle_id = 'lmb_a'",
            "UPDATE label_mapping_bundles SET status = 'draft' WHERE mapping_bundle_id = 'lmb_a'",
            "UPDATE label_mapping_bundles SET status = 'published', "
            "canonical_manifest_sha256 = 'changed-at-publish' "
            "WHERE mapping_bundle_id = 'lmb_approved'",
            "DELETE FROM label_mapping_bundle_paths WHERE bundle_path_id = 'lmbp_a'",
            "UPDATE release_bundle_head_events SET action = 'rollback' "
            "WHERE head_event_id = 'rbhe_a'",
        )
        for statement in invalid_statements:
            with pytest.raises(DBAPIError):
                with engine.begin() as connection:
                    connection.execute(text(statement))

        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO label_mapping_items "
                        "(mapping_item_id, tenant_id, project_id, mapping_version_id, "
                        "source_label_version_id, target_label_version_id, source_label_id, "
                        "relation, compatibility, comparability_status, "
                        "allowed_metric_families, requires_recompute, content_sha256, "
                        "trace_id, payload) VALUES "
                        "('lmi_late', 'tenant_a', 'project_a', 'lmv_a', 'lv_source_a', "
                        "'lv_target_a', 'label_late', 'replace', 'structural-break', "
                        "'structural-break', :families, 0, :sha, 'trace_late', :payload)"
                    ),
                    {
                        "families": json.dumps([]),
                        "sha": "0" * 64,
                        "payload": json.dumps({}),
                    },
                )

        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO label_mapping_items "
                        "(mapping_item_id, tenant_id, project_id, mapping_version_id, "
                        "source_label_version_id, target_label_version_id, source_label_id, "
                        "relation, compatibility, comparability_status, "
                        "allowed_metric_families, requires_recompute, content_sha256, "
                        "trace_id, payload) VALUES "
                        "('lmi_invalid_split', 'tenant_a', 'project_a', 'lmv_a', "
                        "'lv_source_a', 'lv_target_a', 'label_old', 'split-recompute', "
                        "'structural-break', 'structural-break', :families, 0, :sha, "
                        "'trace_invalid_split', :payload)"
                    ),
                    {
                        "families": json.dumps([]),
                        "sha": "8" * 64,
                        "payload": json.dumps({}),
                    },
                )
    finally:
        engine.dispose()
