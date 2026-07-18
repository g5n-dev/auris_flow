"""enforce project-scoped insight causal references

Revision ID: 0014_insight_causal_foreign_keys
Revises: 0013_human_review_single_terminal
Create Date: 2026-07-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0014_insight_causal_foreign_keys"
down_revision = "0013_human_review_single_terminal"
branch_labels = None
depends_on = None


SCOPE_UNIQUE_CONSTRAINTS: tuple[tuple[str, str, str], ...] = (
    ("run_records", "run_id", "uq_run_records_scope_id"),
    # metric_results already has uq_metric_results_scope from migration 0002.
    ("insight_reports", "report_id", "uq_insight_reports_scope_id"),
    ("insight_actions", "action_id", "uq_insight_actions_scope_id"),
    ("insight_experiments", "experiment_id", "uq_insight_experiments_scope_id"),
    ("insight_effects", "effect_id", "uq_insight_effects_scope_id"),
)

CAUSAL_REFERENCES: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "insight_reports",
        "run_id",
        "run_records",
        "run_id",
        "fk_insight_reports_scope_run",
    ),
    (
        "insight_actions",
        "report_id",
        "insight_reports",
        "report_id",
        "fk_insight_actions_scope_report",
    ),
    (
        "insight_actions",
        "baseline_metric_result_id",
        "metric_results",
        "metric_result_id",
        "fk_insight_actions_scope_baseline_metric",
    ),
    (
        "insight_experiments",
        "action_id",
        "insight_actions",
        "action_id",
        "fk_insight_experiments_scope_action",
    ),
    (
        "insight_experiments",
        "eval_run_id",
        "run_records",
        "run_id",
        "fk_insight_experiments_scope_run",
    ),
    (
        "insight_experiments",
        "baseline_metric_result_id",
        "metric_results",
        "metric_result_id",
        "fk_insight_experiments_scope_baseline_metric",
    ),
    (
        "insight_experiments",
        "outcome_metric_result_id",
        "metric_results",
        "metric_result_id",
        "fk_insight_experiments_scope_outcome_metric",
    ),
    (
        "insight_effects",
        "action_id",
        "insight_actions",
        "action_id",
        "fk_insight_effects_scope_action",
    ),
    (
        "insight_effects",
        "experiment_id",
        "insight_experiments",
        "experiment_id",
        "fk_insight_effects_scope_experiment",
    ),
    (
        "insight_effects",
        "baseline_metric_result_id",
        "metric_results",
        "metric_result_id",
        "fk_insight_effects_scope_baseline_metric",
    ),
    (
        "insight_effects",
        "outcome_metric_result_id",
        "metric_results",
        "metric_result_id",
        "fk_insight_effects_scope_outcome_metric",
    ),
)


def _table(name: str, business_id: str) -> sa.TableClause:
    return sa.table(
        name,
        sa.column("tenant_id", sa.String(64)),
        sa.column("project_id", sa.String(64)),
        sa.column(business_id, sa.String(128)),
    )


def _assert_scope_unique(table_name: str, business_id: str) -> None:
    table = _table(table_name, business_id)
    duplicate = (
        op.get_bind()
        .execute(
            sa.select(
                table.c.tenant_id,
                table.c.project_id,
                table.c[business_id],
                sa.func.count().label("row_count"),
            )
            .group_by(table.c.tenant_id, table.c.project_id, table.c[business_id])
            .having(sa.func.count() > 1)
            .limit(1)
        )
        .mappings()
        .first()
    )
    if duplicate is not None:
        raise RuntimeError(
            f"{table_name} contains duplicate scoped ID before causal constraints: "
            f"{dict(duplicate)}"
        )


def _assert_reference_integrity(
    child_name: str,
    child_id: str,
    parent_name: str,
    parent_id: str,
    constraint_name: str,
) -> None:
    child = _table(child_name, child_id).alias("child")
    parent = _table(parent_name, parent_id).alias("parent")
    broken = (
        op.get_bind()
        .execute(
            sa.select(
                child.c.tenant_id,
                child.c.project_id,
                child.c[child_id].label("referenced_id"),
            )
            .select_from(
                child.outerjoin(
                    parent,
                    sa.and_(
                        child.c.tenant_id == parent.c.tenant_id,
                        child.c.project_id == parent.c.project_id,
                        child.c[child_id] == parent.c[parent_id],
                    ),
                )
            )
            .where(child.c[child_id].is_not(None), parent.c[parent_id].is_(None))
            .limit(5)
        )
        .mappings()
        .all()
    )
    if broken:
        samples = ", ".join(
            f"{row['tenant_id']}/{row['project_id']}/{row['referenced_id']}" for row in broken
        )
        raise RuntimeError(
            f"cannot create {constraint_name}; {child_name}.{child_id} has missing or "
            f"cross-scope {parent_name}.{parent_id} references: {samples}"
        )


def _create_scope_unique(
    table_name: str,
    business_id: str,
    constraint_name: str,
) -> None:
    with op.batch_alter_table(table_name) as batch:
        batch.create_unique_constraint(
            constraint_name,
            ["tenant_id", "project_id", business_id],
        )


def _create_foreign_keys(
    table_name: str,
    references: Sequence[tuple[str, str, str, str, str]],
    indexes: Sequence[tuple[str, str]],
) -> None:
    with op.batch_alter_table(table_name) as batch:
        for index_name, column_name in indexes:
            batch.create_index(
                index_name,
                ["tenant_id", "project_id", column_name],
                unique=False,
            )
        for _, child_id, parent_name, parent_id, constraint_name in references:
            batch.create_foreign_key(
                constraint_name,
                parent_name,
                ["tenant_id", "project_id", child_id],
                ["tenant_id", "project_id", parent_id],
                ondelete="RESTRICT",
                onupdate="RESTRICT",
            )


def upgrade() -> None:
    for table_name, business_id, _ in SCOPE_UNIQUE_CONSTRAINTS:
        _assert_scope_unique(table_name, business_id)
    _assert_scope_unique("metric_results", "metric_result_id")
    for reference in CAUSAL_REFERENCES:
        _assert_reference_integrity(*reference)

    for constraint in SCOPE_UNIQUE_CONSTRAINTS:
        _create_scope_unique(*constraint)

    _create_foreign_keys(
        "insight_reports",
        tuple(reference for reference in CAUSAL_REFERENCES if reference[0] == "insight_reports"),
        (),
    )
    _create_foreign_keys(
        "insight_actions",
        tuple(reference for reference in CAUSAL_REFERENCES if reference[0] == "insight_actions"),
        (
            (
                "ix_insight_actions_scope_baseline_metric",
                "baseline_metric_result_id",
            ),
        ),
    )
    _create_foreign_keys(
        "insight_experiments",
        tuple(
            reference for reference in CAUSAL_REFERENCES if reference[0] == "insight_experiments"
        ),
        (
            ("ix_insight_experiments_scope_action", "action_id"),
            (
                "ix_insight_experiments_scope_baseline_metric",
                "baseline_metric_result_id",
            ),
            (
                "ix_insight_experiments_scope_outcome_metric",
                "outcome_metric_result_id",
            ),
        ),
    )
    _create_foreign_keys(
        "insight_effects",
        tuple(reference for reference in CAUSAL_REFERENCES if reference[0] == "insight_effects"),
        (
            (
                "ix_insight_effects_scope_baseline_metric",
                "baseline_metric_result_id",
            ),
            (
                "ix_insight_effects_scope_outcome_metric",
                "outcome_metric_result_id",
            ),
        ),
    )


def _drop_foreign_keys(
    table_name: str,
    references: Sequence[tuple[str, str, str, str, str]],
    indexes: Sequence[str],
) -> None:
    with op.batch_alter_table(table_name) as batch:
        for *_, constraint_name in reversed(references):
            batch.drop_constraint(constraint_name, type_="foreignkey")
        for index_name in reversed(indexes):
            batch.drop_index(index_name)


def downgrade() -> None:
    _drop_foreign_keys(
        "insight_effects",
        tuple(reference for reference in CAUSAL_REFERENCES if reference[0] == "insight_effects"),
        (
            "ix_insight_effects_scope_baseline_metric",
            "ix_insight_effects_scope_outcome_metric",
        ),
    )
    _drop_foreign_keys(
        "insight_experiments",
        tuple(
            reference for reference in CAUSAL_REFERENCES if reference[0] == "insight_experiments"
        ),
        (
            "ix_insight_experiments_scope_action",
            "ix_insight_experiments_scope_baseline_metric",
            "ix_insight_experiments_scope_outcome_metric",
        ),
    )
    _drop_foreign_keys(
        "insight_actions",
        tuple(reference for reference in CAUSAL_REFERENCES if reference[0] == "insight_actions"),
        ("ix_insight_actions_scope_baseline_metric",),
    )
    _drop_foreign_keys(
        "insight_reports",
        tuple(reference for reference in CAUSAL_REFERENCES if reference[0] == "insight_reports"),
        (),
    )

    for table_name, _, constraint_name in reversed(SCOPE_UNIQUE_CONSTRAINTS):
        with op.batch_alter_table(table_name) as batch:
            batch.drop_constraint(constraint_name, type_="unique")
