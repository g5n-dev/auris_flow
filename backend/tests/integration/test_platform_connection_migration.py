from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect, text

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REVISION_BEFORE = "0045_audio_import_batches"
REVISION_PLATFORM_CONNECTIONS = "0046_platform_connections"


def _alembic(database_url: str, *arguments: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", *arguments],
        cwd=BACKEND_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        check=True,
        capture_output=True,
        text=True,
    )


def test_0046_creates_strong_platform_connections_and_scrubs_legacy_secrets(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'platform-connections.sqlite'}"
    _alembic(database_url, "upgrade", REVISION_BEFORE)
    engine = create_engine(database_url, future=True)
    legacy_payload = {
        "connector_id": "legacy_platform_connection",
        "type": "platform_auth",
        "name": "历史平台连接",
        "auth_mode": "session_token",
        "base_url": "https://legacy.example.test",
        "credential_ref": "secret://legacy/platform-reader",
        "platform_scope": {
            "tenant_ref": "legacy-tenant",
            "store_refs": ["legacy-store"],
        },
        "configuration": {
            "password": "legacy-password",
            "nested": {
                "authorization": "Bearer legacy-token",
                "accessToken": "legacy-access-token",
                "clientSecret": "legacy-client-secret",
                "refreshToken": "legacy-refresh-token",
            },
        },
        "status": "success",
        "trace_id": "trace_legacy_platform_connection",
    }
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO json_resources "
                    "(collection, resource_key, tenant_id, project_id, status, trace_id, data) "
                    "VALUES (:collection, :resource_key, :tenant_id, :project_id, "
                    ":status, :trace_id, :data)"
                ),
                {
                    "collection": "connectors",
                    "resource_key": "legacy_platform_connection",
                    "tenant_id": "tenant_migration",
                    "project_id": "project_migration",
                    "status": "success",
                    "trace_id": "trace_legacy_platform_connection",
                    "data": json.dumps(legacy_payload),
                },
            )
            second_platform_payload = {
                **legacy_payload,
                "name": "另一租户的同名平台连接",
                "platform_scope": {
                    "tenant_ref": "other-tenant",
                    "store_refs": ["other-store"],
                },
                "trace_id": "trace_other_platform_connection",
            }
            connection.execute(
                text(
                    "INSERT INTO json_resources "
                    "(collection, resource_key, tenant_id, project_id, status, trace_id, data) "
                    "VALUES (:collection, :resource_key, :tenant_id, :project_id, "
                    ":status, :trace_id, :data)"
                ),
                [
                    {
                        "collection": "connectors",
                        "resource_key": "other_legacy_platform_projection",
                        "tenant_id": "tenant_other",
                        "project_id": "project_other",
                        "status": "success",
                        "trace_id": "trace_other_platform_connection",
                        "data": json.dumps(second_platform_payload),
                    },
                    {
                        "collection": "connectors",
                        "resource_key": "other_audio_import_connector",
                        "tenant_id": "tenant_other",
                        "project_id": "project_other",
                        "status": "draft",
                        "trace_id": "trace_other_audio_import",
                        "data": json.dumps(
                            {
                                "connector_id": "other_audio_import_connector",
                                "source_type": "platform_audio_url_api",
                                "platform_connection_id": "legacy_platform_connection",
                                "credential_ref": "Bearer legacy-live-token",
                            }
                        ),
                    },
                    {
                        "collection": "settings",
                        "resource_key": "legacy_provider_settings",
                        "tenant_id": "tenant_other",
                        "project_id": "project_other",
                        "status": "active",
                        "trace_id": "trace_other_settings",
                        "data": json.dumps(
                            {
                                "provider": "legacy",
                                "configuration": {
                                    "clientSecret": "legacy-client-secret",
                                },
                            }
                        ),
                    },
                ],
            )
            connector_outbox_payload = {
                "connector_id": "legacy_platform_connection",
                "credential_ref": "secret://legacy/platform-reader",
                "configuration": {
                    "apiKey": "legacy-api-key",
                    "nested": [
                        {
                            "Authorization": "Bearer legacy-outbox-token",
                            "safe": "keep-me",
                        }
                    ],
                },
            }
            non_connector_outbox_payload = {
                "invoice_id": "invoice_legacy",
                "configuration": {"password": "unrelated-history-must-not-change"},
            }
            connection.execute(
                text(
                    "INSERT INTO outbox_events "
                    "(tenant_id, project_id, event_type, aggregate_type, aggregate_id, "
                    "status, payload, dispatch_idempotency_key) "
                    "VALUES (:tenant_id, :project_id, :event_type, :aggregate_type, "
                    ":aggregate_id, :status, :payload, :dispatch_idempotency_key)"
                ),
                [
                    {
                        "tenant_id": "tenant_migration",
                        "project_id": "project_migration",
                        "event_type": "connectors.created",
                        "aggregate_type": "connectors",
                        "aggregate_id": "legacy_platform_connection",
                        "status": "pending",
                        "payload": json.dumps(connector_outbox_payload),
                        "dispatch_idempotency_key": "legacy_connector_created",
                    },
                    {
                        "tenant_id": "tenant_migration",
                        "project_id": "project_migration",
                        "event_type": "invoices.created",
                        "aggregate_type": "invoices",
                        "aggregate_id": "invoice_legacy",
                        "status": "pending",
                        "payload": json.dumps(non_connector_outbox_payload),
                        "dispatch_idempotency_key": "legacy_invoice_created",
                    },
                ],
            )
            connector_idempotency_response = {
                "data": {
                    "connector_id": "legacy_platform_connection",
                    "credential_ref": "Bearer should-be-removed",
                    "configuration": {
                        "clientSecret": "legacy-idempotency-secret",
                        "items": [{"refresh-token": "legacy-refresh-token"}],
                    },
                }
            }
            non_connector_idempotency_response = {
                "data": {
                    "setting_id": "setting_legacy",
                    "configuration": {"token": "unrelated-history-must-not-change"},
                }
            }
            connection.execute(
                text(
                    "INSERT INTO idempotency_records "
                    "(tenant_id, project_id, user_id, operation, idempotency_key, "
                    "request_hash, status_code, response_json) "
                    "VALUES (:tenant_id, :project_id, :user_id, :operation, "
                    ":idempotency_key, :request_hash, :status_code, :response_json)"
                ),
                [
                    {
                        "tenant_id": "tenant_migration",
                        "project_id": "project_migration",
                        "user_id": "migration-user",
                        "operation": "connectors.create",
                        "idempotency_key": "legacy-connector-idempotency",
                        "request_hash": "a" * 64,
                        "status_code": 201,
                        "response_json": json.dumps(connector_idempotency_response),
                    },
                    {
                        "tenant_id": "tenant_migration",
                        "project_id": "project_migration",
                        "user_id": "migration-user",
                        "operation": "settings.create",
                        "idempotency_key": "legacy-setting-idempotency",
                        "request_hash": "b" * 64,
                        "status_code": 201,
                        "response_json": json.dumps(non_connector_idempotency_response),
                    },
                ],
            )
            plural_connector_audit_before = {
                "connector_id": "legacy_platform_connection",
                "configuration": {
                    "password": "legacy-audit-password",
                    "safe": "before-safe",
                },
            }
            plural_connector_audit_after = {
                "connector_id": "legacy_platform_connection",
                "credential_ref": "secret://legacy/platform-reader",
                "configuration": {
                    "accessToken": "legacy-audit-access-token",
                    "safe": "after-safe",
                },
            }
            singular_connector_audit_after = {
                "connector_id": "legacy_singular_connector",
                "configuration": [
                    {
                        "client-secret": "legacy-audit-client-secret",
                        "safe": "singular-safe",
                    }
                ],
            }
            non_connector_audit_before = {
                "setting_id": "setting_legacy",
                "configuration": {"password": "non-connector-audit-must-not-change"},
            }
            connector_probe_audit_after = {
                "connector_id": "legacy_platform_connection",
                "observation": {"token": "non-create-patch-audit-must-not-change"},
            }
            mismatched_connector_action_after = {
                "setting_id": "setting_with_legacy_action",
                "configuration": {"secret": "non-connector-object-must-not-change"},
            }
            connection.execute(
                text(
                    "INSERT INTO audit_logs "
                    "(tenant_id, project_id, actor_id, action, object_type, object_id, "
                    "result, trace_id, idempotency_key, before_json, after_json) "
                    "VALUES (:tenant_id, :project_id, :actor_id, :action, :object_type, "
                    ":object_id, :result, :trace_id, :idempotency_key, "
                    ":before_json, :after_json)"
                ),
                [
                    {
                        "tenant_id": "tenant_migration",
                        "project_id": "project_migration",
                        "actor_id": "migration-user",
                        "action": "connectors.patch",
                        "object_type": "connectors",
                        "object_id": "legacy_platform_connection",
                        "result": "success",
                        "trace_id": "trace_connector_patch_audit",
                        "idempotency_key": "legacy-connector-patch-audit",
                        "before_json": json.dumps(plural_connector_audit_before),
                        "after_json": json.dumps(plural_connector_audit_after),
                    },
                    {
                        "tenant_id": "tenant_migration",
                        "project_id": "project_migration",
                        "actor_id": "migration-user",
                        "action": "connector.create",
                        "object_type": "connector",
                        "object_id": "legacy_singular_connector",
                        "result": "success",
                        "trace_id": "trace_connector_create_audit",
                        "idempotency_key": "legacy-connector-create-audit",
                        "before_json": None,
                        "after_json": json.dumps(singular_connector_audit_after),
                    },
                    {
                        "tenant_id": "tenant_migration",
                        "project_id": "project_migration",
                        "actor_id": "migration-user",
                        "action": "settings.patch",
                        "object_type": "settings",
                        "object_id": "setting_legacy",
                        "result": "success",
                        "trace_id": "trace_setting_patch_audit",
                        "idempotency_key": "legacy-setting-patch-audit",
                        "before_json": json.dumps(non_connector_audit_before),
                        "after_json": None,
                    },
                    {
                        "tenant_id": "tenant_migration",
                        "project_id": "project_migration",
                        "actor_id": "migration-user",
                        "action": "connectors.connection_test",
                        "object_type": "connector",
                        "object_id": "legacy_platform_connection",
                        "result": "success",
                        "trace_id": "trace_connector_probe_audit",
                        "idempotency_key": "legacy-connector-probe-audit",
                        "before_json": None,
                        "after_json": json.dumps(connector_probe_audit_after),
                    },
                    {
                        "tenant_id": "tenant_migration",
                        "project_id": "project_migration",
                        "actor_id": "migration-user",
                        "action": "connectors.patch",
                        "object_type": "settings",
                        "object_id": "setting_with_legacy_action",
                        "result": "success",
                        "trace_id": "trace_mismatched_connector_action_audit",
                        "idempotency_key": "legacy-mismatched-connector-action-audit",
                        "before_json": None,
                        "after_json": json.dumps(mismatched_connector_action_after),
                    },
                ],
            )
    finally:
        engine.dispose()

    _alembic(database_url, "upgrade", REVISION_PLATFORM_CONNECTIONS)
    engine = create_engine(database_url, future=True)
    try:
        schema = inspect(engine)
        assert "platform_connections" in schema.get_table_names()
        columns = {column["name"] for column in schema.get_columns("platform_connections")}
        assert {
            "platform_connection_id",
            "tenant_id",
            "project_id",
            "external_tenant_ref",
            "provider_type",
            "auth_mode",
            "origin",
            "credential_ref",
            "status",
            "resource_version",
            "last_tested_at",
            "root_trace_id",
            "current_trace_id",
        }.issubset(columns)
        with engine.connect() as connection:
            migrated = (
                connection.execute(
                    text(
                        "SELECT platform_connection_id, origin, credential_ref, status "
                        "FROM platform_connections "
                        "WHERE platform_connection_id = 'legacy_platform_connection'"
                    )
                )
                .mappings()
                .one()
            )
            assert migrated["origin"] == "https://legacy.example.test"
            assert migrated["credential_ref"] == "secret://legacy/platform-reader"
            assert migrated["status"] == "active"
            scoped_connections = list(
                connection.execute(
                    text(
                        "SELECT platform_connection_id, tenant_id, project_id "
                        "FROM platform_connections ORDER BY tenant_id"
                    )
                ).mappings()
            )
            assert len(scoped_connections) == 2
            assert len({row["platform_connection_id"] for row in scoped_connections}) == 2
            other_connection_id = next(
                row["platform_connection_id"]
                for row in scoped_connections
                if row["tenant_id"] == "tenant_other"
            )
            assert other_connection_id != "legacy_platform_connection"
            other_audio_raw = connection.scalar(
                text(
                    "SELECT data FROM json_resources "
                    "WHERE tenant_id = 'tenant_other' "
                    "AND resource_key = 'other_audio_import_connector'"
                )
            )
            other_audio = (
                json.loads(other_audio_raw) if isinstance(other_audio_raw, str) else other_audio_raw
            )
            assert other_audio["platform_connection_id"] == other_connection_id
            assert "credential_ref" not in other_audio
            assert other_audio["redacted_fields"] == ["credential_ref"]

            settings_raw = connection.scalar(
                text(
                    "SELECT data FROM json_resources "
                    "WHERE resource_key = 'legacy_provider_settings'"
                )
            )
            settings = json.loads(settings_raw) if isinstance(settings_raw, str) else settings_raw
            assert "clientSecret" not in settings["configuration"]
            assert settings["configuration"]["redacted_fields"] == ["clientSecret"]

            scrubbed_raw = connection.scalar(
                text(
                    "SELECT data FROM json_resources "
                    "WHERE resource_key = 'legacy_platform_connection'"
                )
            )
            scrubbed = json.loads(scrubbed_raw) if isinstance(scrubbed_raw, str) else scrubbed_raw
            assert "password" not in scrubbed["configuration"]
            assert "authorization" not in scrubbed["configuration"]["nested"]
            assert "accessToken" not in scrubbed["configuration"]["nested"]
            assert "clientSecret" not in scrubbed["configuration"]["nested"]
            assert "refreshToken" not in scrubbed["configuration"]["nested"]
            assert scrubbed["configuration"]["nested"]["redacted_fields"] == [
                "accessToken",
                "authorization",
                "clientSecret",
                "refreshToken",
            ]
            assert scrubbed["configuration"]["redacted_fields"] == ["password"]
            connector_outbox_raw = connection.scalar(
                text(
                    "SELECT payload FROM outbox_events "
                    "WHERE dispatch_idempotency_key = 'legacy_connector_created'"
                )
            )
            connector_outbox = (
                json.loads(connector_outbox_raw)
                if isinstance(connector_outbox_raw, str)
                else connector_outbox_raw
            )
            assert connector_outbox["credential_ref"] == "secret://legacy/platform-reader"
            assert "apiKey" not in connector_outbox["configuration"]
            assert "Authorization" not in connector_outbox["configuration"]["nested"][0]
            assert connector_outbox["configuration"]["nested"][0]["safe"] == "keep-me"
            assert connector_outbox["configuration"]["nested"][0]["redacted_fields"] == [
                "Authorization"
            ]
            assert connector_outbox["configuration"]["redacted_fields"] == ["apiKey"]

            non_connector_outbox_raw = connection.scalar(
                text(
                    "SELECT payload FROM outbox_events "
                    "WHERE dispatch_idempotency_key = 'legacy_invoice_created'"
                )
            )
            non_connector_outbox = (
                json.loads(non_connector_outbox_raw)
                if isinstance(non_connector_outbox_raw, str)
                else non_connector_outbox_raw
            )
            assert non_connector_outbox == non_connector_outbox_payload

            connector_idempotency_raw = connection.scalar(
                text(
                    "SELECT response_json FROM idempotency_records "
                    "WHERE idempotency_key = 'legacy-connector-idempotency'"
                )
            )
            connector_idempotency = (
                json.loads(connector_idempotency_raw)
                if isinstance(connector_idempotency_raw, str)
                else connector_idempotency_raw
            )
            assert "credential_ref" not in connector_idempotency["data"]
            assert "clientSecret" not in connector_idempotency["data"]["configuration"]
            assert "refresh-token" not in connector_idempotency["data"]["configuration"]["items"][0]
            assert connector_idempotency["data"]["redacted_fields"] == ["credential_ref"]
            assert connector_idempotency["data"]["configuration"]["redacted_fields"] == [
                "clientSecret"
            ]
            assert connector_idempotency["data"]["configuration"]["items"][0][
                "redacted_fields"
            ] == ["refresh-token"]

            non_connector_idempotency_raw = connection.scalar(
                text(
                    "SELECT response_json FROM idempotency_records "
                    "WHERE idempotency_key = 'legacy-setting-idempotency'"
                )
            )
            non_connector_idempotency = (
                json.loads(non_connector_idempotency_raw)
                if isinstance(non_connector_idempotency_raw, str)
                else non_connector_idempotency_raw
            )
            assert non_connector_idempotency == non_connector_idempotency_response

            plural_connector_audit = (
                connection.execute(
                    text(
                        "SELECT before_json, after_json FROM audit_logs "
                        "WHERE idempotency_key = 'legacy-connector-patch-audit'"
                    )
                )
                .mappings()
                .one()
            )
            plural_audit_before = (
                json.loads(plural_connector_audit["before_json"])
                if isinstance(plural_connector_audit["before_json"], str)
                else plural_connector_audit["before_json"]
            )
            plural_audit_after = (
                json.loads(plural_connector_audit["after_json"])
                if isinstance(plural_connector_audit["after_json"], str)
                else plural_connector_audit["after_json"]
            )
            assert "password" not in plural_audit_before["configuration"]
            assert plural_audit_before["configuration"]["safe"] == "before-safe"
            assert plural_audit_before["configuration"]["redacted_fields"] == ["password"]
            assert plural_audit_after["credential_ref"] == "secret://legacy/platform-reader"
            assert "accessToken" not in plural_audit_after["configuration"]
            assert plural_audit_after["configuration"]["safe"] == "after-safe"
            assert plural_audit_after["configuration"]["redacted_fields"] == ["accessToken"]

            singular_connector_audit_raw = connection.scalar(
                text(
                    "SELECT after_json FROM audit_logs "
                    "WHERE idempotency_key = 'legacy-connector-create-audit'"
                )
            )
            singular_connector_audit = (
                json.loads(singular_connector_audit_raw)
                if isinstance(singular_connector_audit_raw, str)
                else singular_connector_audit_raw
            )
            assert "client-secret" not in singular_connector_audit["configuration"][0]
            assert singular_connector_audit["configuration"][0]["safe"] == "singular-safe"
            assert singular_connector_audit["configuration"][0]["redacted_fields"] == [
                "client-secret"
            ]

            untouched_audits = (
                connection.execute(
                    text(
                        "SELECT idempotency_key, before_json, after_json FROM audit_logs "
                        "WHERE idempotency_key IN "
                        "('legacy-setting-patch-audit', "
                        "'legacy-connector-probe-audit', "
                        "'legacy-mismatched-connector-action-audit')"
                    )
                )
                .mappings()
                .all()
            )
            untouched_by_key = {row["idempotency_key"]: row for row in untouched_audits}
            setting_audit_before_raw = untouched_by_key["legacy-setting-patch-audit"]["before_json"]
            setting_audit_before = (
                json.loads(setting_audit_before_raw)
                if isinstance(setting_audit_before_raw, str)
                else setting_audit_before_raw
            )
            connector_probe_after_raw = untouched_by_key["legacy-connector-probe-audit"][
                "after_json"
            ]
            connector_probe_after = (
                json.loads(connector_probe_after_raw)
                if isinstance(connector_probe_after_raw, str)
                else connector_probe_after_raw
            )
            mismatched_action_after_raw = untouched_by_key[
                "legacy-mismatched-connector-action-audit"
            ]["after_json"]
            mismatched_action_after = (
                json.loads(mismatched_action_after_raw)
                if isinstance(mismatched_action_after_raw, str)
                else mismatched_action_after_raw
            )
            assert setting_audit_before == non_connector_audit_before
            assert connector_probe_after == connector_probe_audit_after
            assert mismatched_action_after == mismatched_connector_action_after
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == REVISION_PLATFORM_CONNECTIONS
            )
    finally:
        engine.dispose()

    _alembic(database_url, "downgrade", REVISION_BEFORE)
    engine = create_engine(database_url, future=True)
    try:
        assert "platform_connections" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_0046_tolerates_optional_history_tables_being_absent(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'platform-connections-no-history.sqlite'}"
    _alembic(database_url, "upgrade", REVISION_BEFORE)
    engine = create_engine(database_url, future=True)
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE outbox_delivery_attempts"))
            connection.execute(text("DROP TABLE outbox_events"))
            connection.execute(text("DROP TABLE idempotency_records"))
            connection.execute(text("DROP TABLE audit_logs"))
    finally:
        engine.dispose()

    _alembic(database_url, "upgrade", REVISION_PLATFORM_CONNECTIONS)
    engine = create_engine(database_url, future=True)
    try:
        schema = inspect(engine)
        assert "platform_connections" in schema.get_table_names()
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == REVISION_PLATFORM_CONNECTIONS
            )
    finally:
        engine.dispose()
