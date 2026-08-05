"""add scoped platform connections and scrub legacy connector credentials

Revision ID: 0046_platform_connections
Revises: 0045_audio_import_batches
Create Date: 2026-07-28
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Any
from urllib.parse import urlsplit

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0046_platform_connections"
down_revision = "0045_audio_import_batches"
branch_labels = None
depends_on = None

_PLATFORM_TYPES = frozenset({"platform_auth", "platform_connection"})
_SAFE_REFERENCE = re.compile(
    r"^secret://[A-Za-z][A-Za-z0-9._-]{0,63}"
    r"(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,127}){1,4}$"
)
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "client_secret",
        "cookie",
        "id_token",
        "password",
        "passwd",
        "refresh_token",
        "secret",
        "secret_ref",
        "token",
    }
)
_SENSITIVE_SUFFIXES = ("apikey", "authorization", "password", "secret", "token")
_CONNECTOR_AUDIT_OBJECT_TYPES = ("connector", "connectors")
_CONNECTOR_AUDIT_ACTIONS = (
    "connector.create",
    "connector.patch",
    "connectors.create",
    "connectors.patch",
)


def utc_datetime_type() -> sa.types.TypeEngine:
    return sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql")


def timestamp_columns() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column(
            "created_at",
            utc_datetime_type(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            utc_datetime_type(),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def _is_https_origin(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value.strip())
    return bool(
        parsed.scheme == "https"
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and parsed.query == ""
        and parsed.fragment == ""
        and parsed.path in {"", "/"}
    )


def _scrub_sensitive(value: object) -> object:
    if isinstance(value, list):
        return [_scrub_sensitive(item) for item in value]
    if not isinstance(value, dict):
        return value
    scrubbed: dict[str, object] = {}
    redacted_fields: list[str] = []
    for raw_key, raw_value in value.items():
        key = str(raw_key)
        normalized = key.casefold().replace("-", "_")
        compact = "".join(character for character in normalized if character.isalnum())
        if compact == "credentialref":
            if not isinstance(raw_value, str) or not _SAFE_REFERENCE.fullmatch(raw_value.strip()):
                redacted_fields.append(key)
                continue
            scrubbed[key] = raw_value.strip()
            continue
        if normalized in _SENSITIVE_KEYS or (compact.endswith(_SENSITIVE_SUFFIXES)):
            redacted_fields.append(key)
            continue
        scrubbed[key] = _scrub_sensitive(raw_value)
    if redacted_fields:
        scrubbed["redacted_fields"] = sorted(redacted_fields)
    return scrubbed


def _json_object(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _json_container(value: object) -> dict[str, Any] | list[Any] | None:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (str, bytes, bytearray)):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, UnicodeDecodeError):
            return None
        return parsed if isinstance(parsed, (dict, list)) else None
    return None


def _reflect_table_with_columns(
    table_name: str,
    *,
    required_columns: frozenset[str],
) -> sa.Table | None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(table_name):
        return None
    available_columns = {str(column["name"]) for column in inspector.get_columns(table_name)}
    if not required_columns.issubset(available_columns):
        return None
    return sa.Table(table_name, sa.MetaData(), autoload_with=bind)


def _scrub_selected_json_rows(
    table: sa.Table,
    *,
    identity_column: str,
    json_column: str,
    predicate: sa.ColumnElement[bool],
) -> None:
    bind = op.get_bind()
    rows = list(
        bind.execute(
            sa.select(
                table.c[identity_column],
                table.c[json_column],
            ).where(predicate)
        ).mappings()
    )
    for row in rows:
        original = _json_container(row[json_column])
        if original is None:
            continue
        scrubbed = _scrub_sensitive(original)
        if scrubbed == original:
            continue
        bind.execute(
            table.update()
            .where(table.c[identity_column] == row[identity_column])
            .values({json_column: scrubbed})
        )


def _scrub_legacy_connector_history() -> None:
    outbox_events = _reflect_table_with_columns(
        "outbox_events",
        required_columns=frozenset(
            {
                "event_id",
                "event_type",
                "aggregate_type",
                "payload",
            }
        ),
    )
    if outbox_events is not None:
        _scrub_selected_json_rows(
            outbox_events,
            identity_column="event_id",
            json_column="payload",
            predicate=sa.or_(
                outbox_events.c.aggregate_type.in_(("connector", "connectors")),
                outbox_events.c.event_type.like("connector.%"),
                outbox_events.c.event_type.like("connectors.%"),
            ),
        )

    idempotency_records = _reflect_table_with_columns(
        "idempotency_records",
        required_columns=frozenset(
            {
                "id",
                "operation",
                "response_json",
            }
        ),
    )
    if idempotency_records is not None:
        _scrub_selected_json_rows(
            idempotency_records,
            identity_column="id",
            json_column="response_json",
            predicate=idempotency_records.c.operation.like("connectors.%"),
        )

    for audit_json_column in ("before_json", "after_json"):
        audit_logs = _reflect_table_with_columns(
            "audit_logs",
            required_columns=frozenset(
                {
                    "audit_id",
                    "object_type",
                    "action",
                    audit_json_column,
                }
            ),
        )
        if audit_logs is None:
            continue
        _scrub_selected_json_rows(
            audit_logs,
            identity_column="audit_id",
            json_column=audit_json_column,
            predicate=sa.and_(
                audit_logs.c.object_type.in_(_CONNECTOR_AUDIT_OBJECT_TYPES),
                audit_logs.c.action.in_(_CONNECTOR_AUDIT_ACTIONS),
            ),
        )


def _bounded_text(value: object, *, default: str, maximum: int) -> str:
    if not isinstance(value, str):
        return default
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        return default
    return normalized


def _globally_unique_connection_id(
    requested_id: str,
    *,
    tenant_id: str,
    project_id: str,
    row_id: int,
    used_ids: set[str],
) -> str:
    if requested_id not in used_ids:
        used_ids.add(requested_id)
        return requested_id
    identity = f"{tenant_id}\n{project_id}\n{requested_id}\n{row_id}".encode()
    suffix = (
        base64.b32encode(hashlib.sha256(identity).digest()).decode("ascii").rstrip("=").lower()[:12]
    )
    candidate = f"{requested_id[:115]}_{suffix}"
    counter = 1
    while candidate in used_ids:
        candidate = f"{requested_id[:111]}_{suffix}_{counter}"
        counter += 1
    used_ids.add(candidate)
    return candidate


def _migrate_legacy_platform_connections(platform_connections: sa.Table) -> None:
    bind = op.get_bind()
    json_resources = sa.table(
        "json_resources",
        sa.column("id", sa.Integer()),
        sa.column("collection", sa.String()),
        sa.column("resource_key", sa.String()),
        sa.column("tenant_id", sa.String()),
        sa.column("project_id", sa.String()),
        sa.column("status", sa.String()),
        sa.column("trace_id", sa.String()),
        sa.column("data", sa.JSON()),
    )
    rows = list(
        bind.execute(
            sa.select(
                json_resources.c.id,
                json_resources.c.collection,
                json_resources.c.resource_key,
                json_resources.c.tenant_id,
                json_resources.c.project_id,
                json_resources.c.status,
                json_resources.c.trace_id,
                json_resources.c.data,
            ).order_by(json_resources.c.id)
        ).mappings()
    )
    used_connection_ids: set[str] = set()
    scoped_connection_ids: dict[tuple[str, str, str], str] = {}
    for row in rows:
        if row["collection"] != "connectors":
            continue
        data = _json_object(row["data"])
        source_type = data.get("type") or data.get("source_type")
        if source_type not in _PLATFORM_TYPES:
            continue

        requested_connection_id = _bounded_text(
            data.get("platform_connection_id") or data.get("connector_id") or row["resource_key"],
            default=f"migrated-platform-connection-{row['id']}",
            maximum=128,
        )
        scope_key = (
            str(row["tenant_id"]),
            str(row["project_id"]),
            requested_connection_id,
        )
        existing_connection_id = scoped_connection_ids.get(scope_key)
        if existing_connection_id is not None:
            continue
        connection_id = _globally_unique_connection_id(
            requested_connection_id,
            tenant_id=scope_key[0],
            project_id=scope_key[1],
            row_id=int(row["id"]),
            used_ids=used_connection_ids,
        )
        scoped_connection_ids[scope_key] = connection_id
        origin_candidate = data.get("origin") or data.get("base_url")
        origin_valid = _is_https_origin(origin_candidate)
        origin = (
            str(origin_candidate).strip().rstrip("/")
            if origin_valid
            else "https://reconfigure.invalid"
        )
        raw_credential_ref = data.get("credential_ref") or data.get("secret_ref")
        credential_valid = bool(
            isinstance(raw_credential_ref, str)
            and _SAFE_REFERENCE.fullmatch(raw_credential_ref.strip())
        )
        credential_ref = (
            str(raw_credential_ref).strip()
            if credential_valid
            else "secret://migration/reconfigure"
        )
        platform_scope = (
            data.get("platform_scope") if isinstance(data.get("platform_scope"), dict) else {}
        )
        external_tenant_ref = _bounded_text(
            data.get("external_tenant_ref") or platform_scope.get("tenant_ref"),
            default="reconfigure",
            maximum=256,
        )
        raw_store_refs = data.get("store_refs") or platform_scope.get("store_refs") or []
        store_refs = (
            [
                item.strip()
                for item in raw_store_refs
                if isinstance(item, str)
                and _SAFE_IDENTIFIER.fullmatch(item.strip())
                and len(item.strip()) <= 256
            ][:100]
            if isinstance(raw_store_refs, list)
            else []
        )
        source_status = str(data.get("status") or row["status"] or "").casefold()
        status = (
            "active"
            if origin_valid and credential_valid and source_status in {"active", "success"}
            else "needs_reconfiguration"
        )
        trace_id = _bounded_text(
            data.get("root_trace_id") or data.get("trace_id") or row["trace_id"],
            default=f"trace_platform_connection_migration_{row['id']}",
            maximum=128,
        )
        provider_type = _bounded_text(
            data.get("provider_type"),
            default="generic_http",
            maximum=64,
        )
        auth_mode = _bounded_text(
            data.get("auth_mode"),
            default="custom_header",
            maximum=64,
        )
        test_path = _bounded_text(data.get("test_path"), default="/", maximum=1024)
        if not test_path.startswith("/") or test_path.startswith("//"):
            test_path = "/"

        bind.execute(
            platform_connections.insert().values(
                platform_connection_id=connection_id,
                tenant_id=row["tenant_id"],
                project_id=row["project_id"],
                external_tenant_ref=external_tenant_ref,
                name=_bounded_text(
                    data.get("name"),
                    default=connection_id,
                    maximum=255,
                ),
                provider_type=provider_type,
                auth_mode=auth_mode,
                origin=origin,
                credential_ref=credential_ref,
                store_refs=store_refs,
                test_path=test_path,
                status=status,
                resource_version=1,
                last_test_status=None,
                last_tested_at=None,
                root_trace_id=trace_id,
                current_trace_id=trace_id,
            )
        )

    # Scrub every historical connector and rewrite scoped references only after
    # the complete collision map is known. This keeps audio-import connectors
    # bound to the strong row created for their own tenant/project.
    for row in rows:
        data = _json_object(row["data"])
        scrubbed = _scrub_sensitive(data)
        source_type = data.get("type") or data.get("source_type")
        raw_connection_id = (
            data.get("platform_connection_id") or data.get("connector_id")
            if source_type in _PLATFORM_TYPES
            else data.get("platform_connection_id")
        )
        if (
            row["collection"] == "connectors"
            and isinstance(raw_connection_id, str)
            and raw_connection_id.strip()
        ):
            resolved = scoped_connection_ids.get(
                (
                    str(row["tenant_id"]),
                    str(row["project_id"]),
                    raw_connection_id.strip(),
                )
            )
            if resolved is not None:
                scrubbed["platform_connection_id"] = resolved
        if scrubbed != data:
            bind.execute(
                json_resources.update()
                .where(json_resources.c.id == row["id"])
                .values(data=scrubbed)
            )


def upgrade() -> None:
    platform_connections = op.create_table(
        "platform_connections",
        sa.Column("platform_connection_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("external_tenant_ref", sa.String(256), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("provider_type", sa.String(64), nullable=False),
        sa.Column("auth_mode", sa.String(64), nullable=False),
        sa.Column("origin", sa.String(2048), nullable=False),
        sa.Column("credential_ref", sa.String(512), nullable=False),
        sa.Column("store_refs", sa.JSON(), nullable=False),
        sa.Column("test_path", sa.String(1024), server_default="/", nullable=False),
        sa.Column("status", sa.String(32), server_default="draft", nullable=False),
        sa.Column("resource_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("last_test_status", sa.String(16), nullable=True),
        sa.Column("last_tested_at", utc_datetime_type(), nullable=True),
        sa.Column("root_trace_id", sa.String(128), nullable=False),
        sa.Column("current_trace_id", sa.String(128), nullable=False),
        *timestamp_columns(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "platform_connection_id",
            name="uq_platform_connections_scope_id",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'disabled', 'error', 'needs_reconfiguration')",
            name="ck_platform_connections_status",
        ),
        sa.CheckConstraint(
            "last_test_status IS NULL OR last_test_status IN ('success', 'failed')",
            name="ck_platform_connections_last_test_status",
        ),
        sa.CheckConstraint(
            "resource_version > 0",
            name="ck_platform_connections_resource_version",
        ),
    )
    op.create_index(
        "ix_platform_connections_scope_status_created",
        "platform_connections",
        ["tenant_id", "project_id", "status", "created_at"],
    )
    op.create_index(
        "ix_platform_connections_tenant_id",
        "platform_connections",
        ["tenant_id"],
    )
    op.create_index(
        "ix_platform_connections_project_id",
        "platform_connections",
        ["project_id"],
    )
    op.create_index(
        "ix_platform_connections_status",
        "platform_connections",
        ["status"],
    )
    op.create_index(
        "ix_platform_connections_root_trace_id",
        "platform_connections",
        ["root_trace_id"],
    )
    op.create_index(
        "ix_platform_connections_current_trace_id",
        "platform_connections",
        ["current_trace_id"],
    )
    _migrate_legacy_platform_connections(platform_connections)
    _scrub_legacy_connector_history()


def downgrade() -> None:
    op.drop_table("platform_connections")
