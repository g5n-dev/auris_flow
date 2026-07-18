"""scope active LabelFact compatibility projection by logical key

Revision ID: 0037_label_fact_logical_active_heads
Revises: 0036_label_metric_snapshot_scopes
Create Date: 2026-07-18

The pre-temporal active-slot index treated every fact for the same
subject/label as one assertion.  Temporal logical keys additionally include
namespace, event/segment and assertion slot, so that legacy index incorrectly
rejected two real events for the same subject.  This forward migration keeps
the compatibility ``status/active_slot`` projection but keys its uniqueness by
the server-derived logical hash.  ``LabelFactHead`` remains the authoritative
current-revision pointer.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0037_label_fact_logical_active_heads"
down_revision = "0036_label_metric_snapshot_scopes"
branch_labels = None
depends_on = None

INDEX_NAME = "uq_label_facts_active_head"
LEGACY_INDEX_COLUMNS = (
    "tenant_id",
    "project_id",
    "subject_scope",
    "subject_key",
    "label_id",
    "active_slot",
)
LOGICAL_INDEX_COLUMNS = (
    "tenant_id",
    "project_id",
    "fact_namespace",
    "logical_key_sha",
    "active_slot",
)


def upgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="label_facts")
    op.create_index(
        INDEX_NAME,
        "label_facts",
        list(LOGICAL_INDEX_COLUMNS),
        unique=True,
    )


def _legacy_index_can_be_restored() -> bool:
    collision = op.get_bind().scalar(
        sa.text(
            "SELECT 1 FROM label_facts "
            "WHERE active_slot = 'active' "
            "GROUP BY tenant_id, project_id, subject_scope, subject_key, label_id "
            "HAVING COUNT(*) > 1 LIMIT 1"
        )
    )
    return collision is None


def downgrade() -> None:
    if not _legacy_index_can_be_restored():
        raise RuntimeError(
            "cannot restore legacy LabelFact active index after distinct-event "
            "logical heads have been written; use a forward compensation migration"
        )
    op.drop_index(INDEX_NAME, table_name="label_facts")
    op.create_index(
        INDEX_NAME,
        "label_facts",
        list(LEGACY_INDEX_COLUMNS),
        unique=True,
    )
