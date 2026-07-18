"""enforce a single terminal decision per human review task

Revision ID: 0013_human_review_single_terminal
Revises: 0012_insight_action_closure
Create Date: 2026-07-10
"""

from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0013_human_review_single_terminal"
down_revision = "0012_insight_action_closure"
branch_labels = None
depends_on = None

TERMINAL_DECISIONS = frozenset(
    {"accepted", "approved", "confirm", "modified", "rejected", "blocked"}
)


def json_type() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(sa.JSON(), "mysql")


def _payload_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return decoded
    return {}


def upgrade() -> None:
    with op.batch_alter_table("human_review_decisions") as batch:
        batch.add_column(sa.Column("review_task_id", sa.String(128), nullable=True))
        batch.add_column(sa.Column("terminal_review_task_id", sa.String(128), nullable=True))

    table = sa.table(
        "human_review_decisions",
        sa.column("decision_id", sa.String(128)),
        sa.column("review_task_id", sa.String(128)),
        sa.column("terminal_review_task_id", sa.String(128)),
        sa.column("payload", json_type()),
    )
    connection = op.get_bind()
    rows = connection.execute(sa.select(table.c.decision_id, table.c.payload)).mappings()
    missing_review_task_ids: list[str] = []
    for row in rows:
        payload = _payload_dict(row["payload"])
        review_task_id = payload.get("review_task_id")
        if not isinstance(review_task_id, str) or not review_task_id:
            missing_review_task_ids.append(str(row["decision_id"]))
            continue
        decision = str(payload.get("decision") or payload.get("review_status") or "")
        connection.execute(
            table.update()
            .where(table.c.decision_id == row["decision_id"])
            .values(
                review_task_id=review_task_id,
                terminal_review_task_id=(
                    review_task_id if decision in TERMINAL_DECISIONS else None
                ),
            )
        )
    if missing_review_task_ids:
        raise RuntimeError(
            "human_review_decisions missing review_task_id in payload: "
            + ", ".join(sorted(missing_review_task_ids))
        )

    with op.batch_alter_table("human_review_decisions") as batch:
        batch.alter_column("review_task_id", existing_type=sa.String(128), nullable=False)
        batch.create_unique_constraint(
            "uq_human_review_decisions_terminal_task",
            ["tenant_id", "project_id", "terminal_review_task_id"],
        )
        batch.create_index(
            "ix_human_review_decisions_scope_task",
            ["tenant_id", "project_id", "review_task_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("human_review_decisions") as batch:
        batch.drop_index("ix_human_review_decisions_scope_task")
        batch.drop_constraint("uq_human_review_decisions_terminal_task", type_="unique")
        batch.drop_column("terminal_review_task_id")
        batch.drop_column("review_task_id")
