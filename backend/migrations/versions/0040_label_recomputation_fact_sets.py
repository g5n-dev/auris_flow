"""add strongly anchored full-recompute runs and candidate fact lineage

Revision ID: 0040_label_recomputation_fact_sets
Revises: 0039_label_fact_append_only_contract
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0040_label_recomputation_fact_sets"
down_revision = "0039_label_fact_append_only_contract"
branch_labels = None
depends_on = None

FACT_INSERT_TRIGGER = "trg_label_facts_contract_insert"
FACT_INSERT_SWAP_TRIGGER = "trg_label_facts_contract_insert_next"


def _utc_datetime() -> sa.types.TypeEngine:
    return sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql")


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column("created_at", _utc_datetime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", _utc_datetime(), server_default=sa.func.now(), nullable=False),
    )


def _drop_fact_triggers() -> None:
    for name in (
        FACT_INSERT_TRIGGER,
        FACT_INSERT_SWAP_TRIGGER,
        "trg_label_facts_no_update",
        "trg_label_facts_no_delete",
    ):
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {name}"))


def _create_fact_insert_trigger(name: str, *, reject_human_aggregate: bool) -> None:
    dialect = op.get_bind().dialect.name
    human_guard = (
        "OR (NEW.source_kind = 'human-decision' AND NEW.aggregate_id IS NOT NULL) "
        if reject_human_aggregate
        else ""
    )
    if dialect == "sqlite":
        op.execute(
            sa.text(
                f"CREATE TRIGGER {name} "
                "BEFORE INSERT ON label_facts "
                "WHEN NEW.source_kind IS NULL OR NEW.status <> 'recorded' "
                "OR NEW.active_slot IS NOT NULL "
                f"{human_guard}"
                "BEGIN SELECT RAISE(ABORT, "
                "'label_facts contract requires recorded rows'); END"
            )
        )
    elif dialect in {"mysql", "mariadb"}:
        op.execute(
            sa.text(
                f"CREATE TRIGGER {name} "
                "BEFORE INSERT ON label_facts FOR EACH ROW "
                "BEGIN IF NEW.source_kind IS NULL OR NEW.status <> 'recorded' "
                "OR NEW.active_slot IS NOT NULL "
                f"{human_guard}"
                "THEN SIGNAL SQLSTATE '45000' "
                "SET MESSAGE_TEXT = 'label_facts contract requires recorded rows'; END IF; END"
            )
        )


def _create_fact_mutation_triggers() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(
                sa.text(
                    f"CREATE TRIGGER trg_label_facts_no_{action.lower()} "
                    f"BEFORE {action} ON label_facts "
                    "BEGIN SELECT RAISE(ABORT, 'append-only label_facts'); END"
                )
            )
    elif dialect in {"mysql", "mariadb"}:
        for action in ("UPDATE", "DELETE"):
            op.execute(
                sa.text(
                    f"CREATE TRIGGER trg_label_facts_no_{action.lower()} "
                    f"BEFORE {action} ON label_facts FOR EACH ROW "
                    "SIGNAL SQLSTATE '45000' "
                    "SET MESSAGE_TEXT = 'append-only label_facts'"
                )
            )


def _create_fact_triggers(*, reject_human_aggregate: bool) -> None:
    _create_fact_insert_trigger(
        FACT_INSERT_TRIGGER,
        reject_human_aggregate=reject_human_aggregate,
    )
    _create_fact_mutation_triggers()


def _prepare_fact_schema_change() -> None:
    # SQLite batch table recreation drops triggers inside one transactional writer
    # lock. MySQL/MariaDB native ALTER preserves them, so keep UPDATE/DELETE guards
    # live throughout the schema change.
    if op.get_bind().dialect.name == "sqlite":
        _drop_fact_triggers()


def _finish_fact_schema_change(*, reject_human_aggregate: bool) -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        _create_fact_triggers(reject_human_aggregate=reject_human_aggregate)
        return
    if dialect in {"mysql", "mariadb"}:
        # Install the next guard before replacing the prior one. At least one
        # BEFORE INSERT guard remains active even if a later DDL statement fails.
        _create_fact_insert_trigger(
            FACT_INSERT_SWAP_TRIGGER,
            reject_human_aggregate=reject_human_aggregate,
        )
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {FACT_INSERT_TRIGGER}"))
        _create_fact_insert_trigger(
            FACT_INSERT_TRIGGER,
            reject_human_aggregate=reject_human_aggregate,
        )
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {FACT_INSERT_SWAP_TRIGGER}"))


def _create_runs() -> None:
    op.create_table(
        "label_recompute_runs",
        sa.Column("recompute_run_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="requested"),
        sa.Column("target_label_version_id", sa.String(128), nullable=False),
        sa.Column("target_resource_version", sa.Integer(), nullable=False),
        sa.Column("target_content_sha256", sa.String(64), nullable=False),
        sa.Column("mapping_bundle_id", sa.String(128), nullable=True),
        sa.Column("mapping_bundle_sha256", sa.String(64), nullable=True),
        sa.Column("source_fact_set_id", sa.String(128), nullable=False),
        sa.Column("source_fact_namespace", sa.String(128), nullable=False),
        sa.Column("source_head_generation", sa.Integer(), nullable=False),
        sa.Column("source_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("candidate_fact_set_id", sa.String(128), nullable=False),
        sa.Column("fact_namespace", sa.String(128), nullable=False),
        sa.Column("fact_as_of", _utc_datetime(), nullable=False),
        sa.Column("partition_scope", sa.JSON(), nullable=False),
        sa.Column("asset_scope", sa.JSON(), nullable=False),
        sa.Column("coverage_policy", sa.JSON(), nullable=False),
        sa.Column("coverage_min", sa.Float(), nullable=False),
        sa.Column("budget", sa.JSON(), nullable=False),
        sa.Column("budget_units", sa.Integer(), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("root_trace_id", sa.String(128), nullable=False),
        sa.Column("action_trace_id", sa.String(128), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "tenant_id", "project_id", "recompute_run_id", name="uq_label_recompute_runs_scope"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "candidate_fact_set_id",
            name="uq_label_recompute_runs_scope_candidate",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "target_label_version_id"],
            [
                "label_versions.tenant_id",
                "label_versions.project_id",
                "label_versions.label_version_id",
            ],
            name="fk_label_recompute_runs_scope_target",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "mapping_bundle_id", "mapping_bundle_sha256"],
            [
                "label_mapping_bundles.tenant_id",
                "label_mapping_bundles.project_id",
                "label_mapping_bundles.mapping_bundle_id",
                "label_mapping_bundles.canonical_manifest_sha256",
            ],
            name="fk_label_recompute_runs_scope_mapping_bundle",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "source_fact_set_id"],
            [
                "label_fact_sets.tenant_id",
                "label_fact_sets.project_id",
                "label_fact_sets.fact_set_id",
            ],
            name="fk_label_recompute_runs_scope_source_fact_set",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "candidate_fact_set_id"],
            [
                "label_fact_sets.tenant_id",
                "label_fact_sets.project_id",
                "label_fact_sets.fact_set_id",
            ],
            name="fk_label_recompute_runs_scope_candidate_fact_set",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('requested', 'running', 'candidate-complete', "
            "'partial-failed', 'failed', 'blocked')",
            name="ck_label_recompute_runs_status",
        ),
        sa.CheckConstraint(
            "(mapping_bundle_id IS NULL AND mapping_bundle_sha256 IS NULL) OR "
            "(mapping_bundle_id IS NOT NULL AND mapping_bundle_sha256 IS NOT NULL)",
            name="ck_label_recompute_runs_mapping_pair",
        ),
        sa.CheckConstraint(
            "source_head_generation > 0 AND budget_units > 0 AND coverage_min >= 0 "
            "AND coverage_min <= 1",
            name="ck_label_recompute_runs_limits",
        ),
        sa.CheckConstraint(
            "LENGTH(source_manifest_sha256) = 64 AND LENGTH(target_content_sha256) = 64 "
            "AND LENGTH(request_sha256) = 64 AND "
            "(mapping_bundle_sha256 IS NULL OR LENGTH(mapping_bundle_sha256) = 64)",
            name="ck_label_recompute_runs_hashes",
        ),
    )
    op.create_index(
        "ix_label_recompute_runs_scope_status",
        "label_recompute_runs",
        ["tenant_id", "project_id", "status"],
    )
    op.create_index("ix_label_recompute_runs_trace_id", "label_recompute_runs", ["trace_id"])


def _create_run_items() -> None:
    op.create_table(
        "label_recompute_run_items",
        sa.Column("recompute_run_item_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("recompute_run_id", sa.String(128), nullable=False),
        sa.Column("partition_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("attempt_generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("execution_run_id", sa.String(128), nullable=False),
        sa.Column("completion_receipt_id", sa.String(128), nullable=True),
        sa.Column("source_manifest_sha256", sa.String(64), nullable=True),
        sa.Column("result_manifest_sha256", sa.String(64), nullable=True),
        sa.Column("content_sha256", sa.String(64), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lineage_manifest", sa.JSON(), nullable=False),
        sa.Column("root_trace_id", sa.String(128), nullable=False),
        sa.Column("action_trace_id", sa.String(128), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "recompute_run_item_id",
            name="uq_label_recompute_run_items_scope",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "recompute_run_id",
            "partition_id",
            name="uq_label_recompute_run_items_partition",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "recompute_run_id"],
            [
                "label_recompute_runs.tenant_id",
                "label_recompute_runs.project_id",
                "label_recompute_runs.recompute_run_id",
            ],
            name="fk_label_recompute_run_items_scope_run",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "execution_run_id"],
            ["run_records.tenant_id", "run_records.project_id", "run_records.run_id"],
            name="fk_label_recompute_run_items_scope_execution",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_label_recompute_run_items_status",
        ),
        sa.CheckConstraint(
            "attempt_generation > 0 AND row_count >= 0",
            name="ck_label_recompute_run_items_counts",
        ),
        sa.CheckConstraint(
            "(status = 'succeeded' AND completion_receipt_id IS NOT NULL "
            "AND source_manifest_sha256 IS NOT NULL AND result_manifest_sha256 IS NOT NULL "
            "AND content_sha256 IS NOT NULL) OR status <> 'succeeded'",
            name="ck_label_recompute_run_items_completion",
        ),
    )
    op.create_index(
        "ix_label_recompute_run_items_scope_status",
        "label_recompute_run_items",
        ["tenant_id", "project_id", "recompute_run_id", "status"],
    )
    op.create_index(
        "ix_label_recompute_run_items_trace_id", "label_recompute_run_items", ["trace_id"]
    )


def _expand_fact_source_union() -> None:
    _prepare_fact_schema_change()
    with op.batch_alter_table("label_facts", recreate="auto") as batch_op:
        batch_op.drop_constraint("ck_label_facts_recompute_reserved", type_="check")
        batch_op.drop_constraint("ck_label_facts_expand_source", type_="check")
        batch_op.alter_column(
            "aggregate_id",
            existing_type=sa.String(128),
            nullable=True,
        )
        batch_op.create_foreign_key(
            "fk_label_facts_scope_recompute_item",
            "label_recompute_run_items",
            ["tenant_id", "project_id", "recompute_run_item_id"],
            ["tenant_id", "project_id", "recompute_run_item_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        )
        batch_op.create_check_constraint(
            "ck_label_facts_expand_source",
            "source_kind IS NULL OR "
            "(source_kind = 'aggregate' AND aggregate_id IS NOT NULL "
            "AND human_review_decision_id IS NULL AND recompute_run_item_id IS NULL) OR "
            "(source_kind = 'human-decision' "
            "AND human_review_decision_id IS NOT NULL AND recompute_run_item_id IS NULL) OR "
            "(source_kind = 'recompute-run-item' AND aggregate_id IS NULL "
            "AND human_review_decision_id IS NULL AND recompute_run_item_id IS NOT NULL)",
        )
    _finish_fact_schema_change(reject_human_aggregate=True)


def upgrade() -> None:
    _create_runs()
    _create_run_items()
    _expand_fact_source_union()


def downgrade() -> None:
    bind = op.get_bind()
    generated = bind.execute(
        sa.text(
            "SELECT recompute_run_item_id FROM label_facts "
            "WHERE source_kind = 'recompute-run-item' LIMIT 1"
        )
    ).first()
    if generated is not None:
        raise RuntimeError(
            "cannot downgrade full-recompute schema after candidate facts exist; "
            f"blocked by recompute_run_item_id={generated[0]}"
        )
    missing = bind.execute(
        sa.text(
            "SELECT fact_id FROM label_facts "
            "WHERE source_kind = 'human-decision' AND aggregate_id IS NULL LIMIT 1"
        )
    ).first()
    if missing is not None:
        raise RuntimeError(
            f"cannot restore legacy non-null aggregate projection; blocked by fact_id={missing[0]}"
        )
    recompute_item = bind.execute(
        sa.text("SELECT recompute_run_item_id FROM label_recompute_run_items LIMIT 1")
    ).first()
    if recompute_item is not None:
        raise RuntimeError(
            "cannot downgrade full-recompute schema without deleting immutable run-item "
            f"history; blocked by recompute_run_item_id={recompute_item[0]}"
        )
    recompute_run = bind.execute(
        sa.text("SELECT recompute_run_id FROM label_recompute_runs LIMIT 1")
    ).first()
    if recompute_run is not None:
        raise RuntimeError(
            "cannot downgrade full-recompute schema without deleting immutable run history; "
            f"blocked by recompute_run_id={recompute_run[0]}"
        )

    # All business-data blockers must be evaluated before MySQL/MariaDB auto-commit
    # DDL or trigger removal.  A failed downgrade must leave the append-only guard
    # and the 0040 schema completely intact.
    _prepare_fact_schema_change()
    with op.batch_alter_table("label_facts", recreate="auto") as batch_op:
        batch_op.drop_constraint("ck_label_facts_expand_source", type_="check")
        batch_op.drop_constraint("fk_label_facts_scope_recompute_item", type_="foreignkey")
    with op.batch_alter_table("label_facts", recreate="auto") as batch_op:
        batch_op.alter_column("aggregate_id", existing_type=sa.String(128), nullable=False)
        batch_op.create_check_constraint(
            "ck_label_facts_recompute_reserved",
            "recompute_run_item_id IS NULL",
        )
        batch_op.create_check_constraint(
            "ck_label_facts_expand_source",
            "source_kind IS NULL OR "
            "(source_kind = 'aggregate' AND aggregate_id IS NOT NULL "
            "AND human_review_decision_id IS NULL) OR "
            "(source_kind = 'human-decision' AND human_review_decision_id IS NOT NULL)",
        )
    _finish_fact_schema_change(reject_human_aggregate=False)
    op.drop_index("ix_label_recompute_run_items_trace_id", table_name="label_recompute_run_items")
    op.drop_index(
        "ix_label_recompute_run_items_scope_status", table_name="label_recompute_run_items"
    )
    op.drop_table("label_recompute_run_items")
    op.drop_index("ix_label_recompute_runs_trace_id", table_name="label_recompute_runs")
    op.drop_index("ix_label_recompute_runs_scope_status", table_name="label_recompute_runs")
    op.drop_table("label_recompute_runs")
