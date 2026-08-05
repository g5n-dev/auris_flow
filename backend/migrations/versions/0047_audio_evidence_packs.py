"""strengthen immutable audio evidence packs

Revision ID: 0047_audio_evidence_packs
Revises: 0046_platform_connections
Create Date: 2026-07-28
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0047_audio_evidence_packs"
down_revision = "0046_platform_connections"
branch_labels = None
depends_on = None

_SHA256 = frozenset("0123456789abcdef")
_ADDED_COLUMNS = (
    "audio_session_id",
    "recording_id",
    "storage_object_id",
    "storage_object_version",
    "audio_sha256",
    "asr_result_id",
    "asr_result_version",
    "window_start_ms",
    "window_end_ms",
    "evidence_sha256",
    "source_run_id",
    "resource_version",
    "root_trace_id",
    "current_trace_id",
)
_ADDED_COLUMN_TYPES = {
    "audio_session_id": sa.String(128),
    "recording_id": sa.String(128),
    "storage_object_id": sa.String(128),
    "storage_object_version": sa.String(512),
    "audio_sha256": sa.String(64),
    "asr_result_id": sa.String(256),
    "asr_result_version": sa.String(128),
    "window_start_ms": sa.Integer(),
    "window_end_ms": sa.Integer(),
    "evidence_sha256": sa.String(64),
    "source_run_id": sa.String(128),
    "resource_version": sa.Integer(),
    "root_trace_id": sa.String(128),
    "current_trace_id": sa.String(128),
}


def _payload(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _text(value: object, fallback: str, maximum: int) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    return normalized[:maximum] if normalized else fallback[:maximum]


def _sha(value: object, fallback_seed: str) -> str:
    normalized = value.strip().casefold() if isinstance(value, str) else ""
    if len(normalized) == 64 and all(character in _SHA256 for character in normalized):
        return normalized
    return hashlib.sha256(fallback_seed.encode("utf-8")).hexdigest()


def _legacy_binding(row: sa.RowMapping) -> dict[str, Any]:
    evidence_pack_id = str(row["evidence_pack_id"])
    payload = _payload(row["payload"])
    storage = payload.get("storage_object")
    storage_payload = storage if isinstance(storage, dict) else {}
    asr = payload.get("asr_result")
    asr_payload = asr if isinstance(asr, dict) else {}
    window = payload.get("time_window")
    window_payload = window if isinstance(window, dict) else {}
    trace_id = _text(
        payload.get("root_trace_id") or row["trace_id"],
        f"migration:{evidence_pack_id}",
        128,
    )
    start = window_payload.get("start_ms")
    end = window_payload.get("end_ms")
    start_ms = start if isinstance(start, int) and not isinstance(start, bool) and start >= 0 else 0
    end_ms = (
        end
        if isinstance(end, int) and not isinstance(end, bool) and end > start_ms
        else start_ms + 1
    )
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "audio_session_id": _text(
            payload.get("audio_session_id"),
            f"legacy-unbound:{evidence_pack_id}",
            128,
        ),
        "recording_id": _text(
            payload.get("recording_id"),
            f"legacy-unbound:{evidence_pack_id}",
            128,
        ),
        "storage_object_id": _text(
            storage_payload.get("storage_object_id") or payload.get("storage_object_id"),
            f"legacy-unbound:{evidence_pack_id}",
            128,
        ),
        "storage_object_version": _text(
            storage_payload.get("version_id"),
            "legacy-unbound",
            512,
        ),
        "audio_sha256": _sha(
            storage_payload.get("content_sha256") or payload.get("audio_sha256"),
            f"legacy-audio:{evidence_pack_id}",
        ),
        "asr_result_id": _text(
            asr_payload.get("asr_result_id"),
            f"legacy-unbound:{evidence_pack_id}",
            256,
        ),
        "asr_result_version": _text(
            asr_payload.get("version"),
            "legacy-unbound",
            128,
        ),
        "window_start_ms": start_ms,
        "window_end_ms": end_ms,
        # Historical JSON rows were not immutable strong evidence. Even when
        # they carried a hash-shaped field, two rows could reuse it. Bind the
        # superseded audit sentinel to the legacy row identity so the new
        # scoped uniqueness constraint can be introduced without data loss.
        "evidence_sha256": hashlib.sha256(
            f"legacy-evidence:{evidence_pack_id}:{canonical}".encode()
        ).hexdigest(),
        "source_run_id": _text(
            payload.get("source_run_id"),
            f"legacy-unbound:{evidence_pack_id}",
            128,
        ),
        "resource_version": 1,
        "root_trace_id": trace_id,
        "current_trace_id": _text(
            payload.get("current_trace_id") or row["trace_id"],
            trace_id,
            128,
        ),
        # A weak historical row is preserved for audit, but can never be
        # mistaken for a production-ready immutable evidence pack.
        "status": "superseded",
    }


def _enforce_added_columns_not_null(batch: Any) -> None:
    for name in _ADDED_COLUMNS:
        alter_options: dict[str, Any] = {
            "existing_type": _ADDED_COLUMN_TYPES[name],
            "existing_nullable": True,
            "nullable": False,
        }
        if name == "resource_version":
            alter_options["existing_server_default"] = "1"
        batch.alter_column(name, **alter_options)


def upgrade() -> None:
    with op.batch_alter_table("evidence_packs") as batch:
        batch.add_column(sa.Column("audio_session_id", sa.String(128), nullable=True))
        batch.add_column(sa.Column("recording_id", sa.String(128), nullable=True))
        batch.add_column(sa.Column("storage_object_id", sa.String(128), nullable=True))
        batch.add_column(sa.Column("storage_object_version", sa.String(512), nullable=True))
        batch.add_column(sa.Column("audio_sha256", sa.String(64), nullable=True))
        batch.add_column(sa.Column("asr_result_id", sa.String(256), nullable=True))
        batch.add_column(sa.Column("asr_result_version", sa.String(128), nullable=True))
        batch.add_column(sa.Column("window_start_ms", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("window_end_ms", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("evidence_sha256", sa.String(64), nullable=True))
        batch.add_column(sa.Column("source_run_id", sa.String(128), nullable=True))
        batch.add_column(
            sa.Column("resource_version", sa.Integer(), server_default="1", nullable=True)
        )
        batch.add_column(sa.Column("root_trace_id", sa.String(128), nullable=True))
        batch.add_column(sa.Column("current_trace_id", sa.String(128), nullable=True))

    bind = op.get_bind()
    table = sa.table(
        "evidence_packs",
        sa.column("evidence_pack_id", sa.String(128)),
        sa.column("status", sa.String(32)),
        sa.column("trace_id", sa.String(128)),
        sa.column("payload", sa.JSON()),
        *(sa.column(name) for name in _ADDED_COLUMNS),
    )
    rows = bind.execute(
        sa.select(
            table.c.evidence_pack_id,
            table.c.trace_id,
            table.c.payload,
        )
    ).mappings()
    for row in rows:
        values = _legacy_binding(row)
        bind.execute(
            table.update()
            .where(table.c.evidence_pack_id == row["evidence_pack_id"])
            .values(**values)
        )

    with op.batch_alter_table("evidence_packs", recreate="auto") as batch:
        _enforce_added_columns_not_null(batch)
        batch.create_unique_constraint(
            "uq_evidence_packs_scope_hash",
            ["tenant_id", "project_id", "evidence_sha256"],
        )
        batch.create_check_constraint(
            "ck_evidence_packs_status",
            "status IN ('ready', 'superseded')",
        )
        batch.create_check_constraint(
            "ck_evidence_packs_hashes",
            "LENGTH(audio_sha256) = 64 AND LENGTH(evidence_sha256) = 64",
        )
        batch.create_check_constraint(
            "ck_evidence_packs_window",
            "window_start_ms >= 0 AND window_end_ms > window_start_ms",
        )
        batch.create_check_constraint(
            "ck_evidence_packs_resource_version",
            "resource_version > 0",
        )

    op.create_index(
        "ix_evidence_packs_scope_audio_session",
        "evidence_packs",
        ["tenant_id", "project_id", "audio_session_id"],
    )
    op.create_index(
        "ix_evidence_packs_scope_recording",
        "evidence_packs",
        ["tenant_id", "project_id", "recording_id"],
    )
    op.create_index(
        "ix_evidence_packs_source_run_id",
        "evidence_packs",
        ["source_run_id"],
    )
    op.create_index(
        "ix_evidence_packs_root_trace_id",
        "evidence_packs",
        ["root_trace_id"],
    )
    op.create_index(
        "ix_evidence_packs_current_trace_id",
        "evidence_packs",
        ["current_trace_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_evidence_packs_current_trace_id", table_name="evidence_packs")
    op.drop_index("ix_evidence_packs_root_trace_id", table_name="evidence_packs")
    op.drop_index("ix_evidence_packs_source_run_id", table_name="evidence_packs")
    op.drop_index("ix_evidence_packs_scope_recording", table_name="evidence_packs")
    op.drop_index("ix_evidence_packs_scope_audio_session", table_name="evidence_packs")
    with op.batch_alter_table("evidence_packs", recreate="auto") as batch:
        batch.drop_constraint("ck_evidence_packs_resource_version", type_="check")
        batch.drop_constraint("ck_evidence_packs_window", type_="check")
        batch.drop_constraint("ck_evidence_packs_hashes", type_="check")
        batch.drop_constraint("ck_evidence_packs_status", type_="check")
        batch.drop_constraint("uq_evidence_packs_scope_hash", type_="unique")
        for name in reversed(_ADDED_COLUMNS):
            batch.drop_column(name)
