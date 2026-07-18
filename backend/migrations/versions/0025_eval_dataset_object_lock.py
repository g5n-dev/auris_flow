"""freeze evaluation dataset manifest object identities

Revision ID: 0025_eval_dataset_object_lock
Revises: 0024_asr_annotation_corrections
Create Date: 2026-07-14
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0025_eval_dataset_object_lock"
down_revision = "0024_asr_annotation_corrections"
branch_labels = None
depends_on = None


def _payload_dict(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        parsed = json.loads(value)
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _canonical_sha256(document: dict[str, Any]) -> str:
    canonical = json.dumps(
        document,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _strong_etag_or_none(value: object) -> str | None:
    raw = str(value or "").strip()
    if not raw or raw[:2].lower() == "w/":
        return None
    if raw.startswith('"') or raw.endswith('"'):
        if len(raw) < 2 or not (raw.startswith('"') and raw.endswith('"')):
            return None
        raw = raw[1:-1]
    if (
        not raw
        or '"' in raw
        or any(ord(character) < 0x21 or ord(character) == 0x7F for character in raw)
    ):
        return None
    return raw


def _eval_dataset_table(*, include_object_snapshot: bool) -> sa.TableClause:
    columns = [
        sa.column("eval_dataset_id", sa.String(128)),
        sa.column("tenant_id", sa.String(64)),
        sa.column("project_id", sa.String(64)),
        sa.column("name", sa.String(255)),
        sa.column("capability", sa.String(64)),
        sa.column("dataset_version", sa.String(64)),
        sa.column("manifest_storage_object_id", sa.String(128)),
        sa.column("manifest_sha256", sa.String(64)),
        sa.column("sample_count", sa.Integer()),
        sa.column("payload", sa.JSON()),
    ]
    if include_object_snapshot:
        columns.extend(
            [
                sa.column("manifest_provider", sa.String(32)),
                sa.column("manifest_bucket", sa.String(255)),
                sa.column("manifest_object_key", sa.String(1024)),
                sa.column("manifest_content_type", sa.String(128)),
                sa.column("manifest_size_bytes", sa.BigInteger()),
                sa.column("manifest_etag", sa.String(255)),
            ]
        )
    return sa.table("eval_dataset_versions", *columns)


def _storage_object_table() -> sa.TableClause:
    return sa.table(
        "storage_objects",
        sa.column("storage_object_id", sa.String(128)),
        sa.column("tenant_id", sa.String(64)),
        sa.column("project_id", sa.String(64)),
        sa.column("provider", sa.String(32)),
        sa.column("bucket", sa.String(255)),
        sa.column("object_key", sa.String(1024)),
        sa.column("content_type", sa.String(128)),
        sa.column("size_bytes", sa.BigInteger()),
        sa.column("etag", sa.String(255)),
    )


def _legacy_snapshot_document(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "eval_dataset_id": row["eval_dataset_id"],
        "name": row["name"],
        "capability": row["capability"],
        "dataset_version": row["dataset_version"],
        "manifest_storage_object_id": row["manifest_storage_object_id"],
        "manifest_sha256": row["manifest_sha256"],
        "sample_count": row["sample_count"],
    }


def _backfill_object_snapshots() -> None:
    bind = op.get_bind()
    datasets = _eval_dataset_table(include_object_snapshot=True)
    storage_objects = _storage_object_table()
    rows = list(
        bind.execute(
            sa.select(
                datasets.c.eval_dataset_id,
                datasets.c.tenant_id,
                datasets.c.project_id,
                datasets.c.name,
                datasets.c.capability,
                datasets.c.dataset_version,
                datasets.c.manifest_storage_object_id,
                datasets.c.manifest_sha256,
                datasets.c.sample_count,
                datasets.c.payload,
                storage_objects.c.provider.label("object_provider"),
                storage_objects.c.bucket.label("object_bucket"),
                storage_objects.c.object_key.label("object_key"),
                storage_objects.c.content_type.label("object_content_type"),
                storage_objects.c.size_bytes.label("object_size_bytes"),
                storage_objects.c.etag.label("object_etag"),
            ).select_from(
                datasets.outerjoin(
                    storage_objects,
                    sa.and_(
                        storage_objects.c.storage_object_id
                        == datasets.c.manifest_storage_object_id,
                        storage_objects.c.tenant_id == datasets.c.tenant_id,
                        storage_objects.c.project_id == datasets.c.project_id,
                    ),
                )
            )
        ).mappings()
    )
    for row_mapping in rows:
        row = dict(row_mapping)
        if row["object_provider"] is None:
            continue
        content_type = str(row["object_content_type"] or "").split(";", 1)[0].strip().lower()
        size_bytes = int(row["object_size_bytes"]) if row["object_size_bytes"] is not None else None
        etag = _strong_etag_or_none(row["object_etag"])
        snapshot_document = {
            **_legacy_snapshot_document(row),
            "manifest_provider": str(row["object_provider"] or "").strip().lower(),
            "manifest_bucket": row["object_bucket"],
            "manifest_object_key": row["object_key"],
            "manifest_content_type": content_type,
            "manifest_size_bytes": size_bytes,
            "manifest_etag": etag,
        }
        payload = _payload_dict(row["payload"])
        payload["snapshot_sha256"] = _canonical_sha256(snapshot_document)
        bind.execute(
            sa.update(datasets)
            .where(
                datasets.c.eval_dataset_id == row["eval_dataset_id"],
                datasets.c.tenant_id == row["tenant_id"],
                datasets.c.project_id == row["project_id"],
            )
            .values(
                manifest_provider=snapshot_document["manifest_provider"],
                manifest_bucket=snapshot_document["manifest_bucket"],
                manifest_object_key=snapshot_document["manifest_object_key"],
                manifest_content_type=snapshot_document["manifest_content_type"],
                manifest_size_bytes=snapshot_document["manifest_size_bytes"],
                manifest_etag=snapshot_document["manifest_etag"],
                payload=payload,
            )
        )


def _restore_legacy_snapshot_hashes() -> None:
    bind = op.get_bind()
    datasets = _eval_dataset_table(include_object_snapshot=True)
    rows = list(bind.execute(sa.select(datasets)).mappings())
    for row_mapping in rows:
        row = dict(row_mapping)
        payload = _payload_dict(row["payload"])
        payload["snapshot_sha256"] = _canonical_sha256(_legacy_snapshot_document(row))
        bind.execute(
            sa.update(datasets)
            .where(
                datasets.c.eval_dataset_id == row["eval_dataset_id"],
                datasets.c.tenant_id == row["tenant_id"],
                datasets.c.project_id == row["project_id"],
            )
            .values(payload=payload)
        )


def upgrade() -> None:
    op.add_column(
        "eval_dataset_versions",
        sa.Column("manifest_provider", sa.String(32), nullable=True),
    )
    op.add_column(
        "eval_dataset_versions",
        sa.Column("manifest_bucket", sa.String(255), nullable=True),
    )
    op.add_column(
        "eval_dataset_versions",
        sa.Column("manifest_object_key", sa.String(1024), nullable=True),
    )
    op.add_column(
        "eval_dataset_versions",
        sa.Column("manifest_content_type", sa.String(128), nullable=True),
    )
    op.add_column(
        "eval_dataset_versions",
        sa.Column("manifest_size_bytes", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "eval_dataset_versions",
        sa.Column("manifest_etag", sa.String(255), nullable=True),
    )
    _backfill_object_snapshots()


def downgrade() -> None:
    _restore_legacy_snapshot_hashes()
    with op.batch_alter_table("eval_dataset_versions") as batch_op:
        batch_op.drop_column("manifest_etag")
        batch_op.drop_column("manifest_size_bytes")
        batch_op.drop_column("manifest_content_type")
        batch_op.drop_column("manifest_object_key")
        batch_op.drop_column("manifest_bucket")
        batch_op.drop_column("manifest_provider")
