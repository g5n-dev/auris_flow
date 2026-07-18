"""separate evaluated hotword current version from production activation

Revision ID: 0023_hotword_production_activation
Revises: 0022_eval_dataset_versions
Create Date: 2026-07-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023_hotword_production_activation"
down_revision = "0022_eval_dataset_versions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "hotword_packs",
        sa.Column("production_version_id", sa.String(128), nullable=True),
    )
    op.create_index(
        "ix_hotword_packs_scope_production_version",
        "hotword_packs",
        ["tenant_id", "project_id", "production_version_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_hotword_packs_scope_production_version",
        table_name="hotword_packs",
    )
    op.drop_column("hotword_packs", "production_version_id")
