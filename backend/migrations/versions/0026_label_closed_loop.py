"""add strong label closed-loop truth tables

Revision ID: 0026_label_closed_loop
Revises: 0025_eval_dataset_object_lock
Create Date: 2026-07-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0026_label_closed_loop"
down_revision = "0025_eval_dataset_object_lock"
branch_labels = None
depends_on = None


def utc_datetime_type() -> sa.types.TypeEngine:
    return sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql")


def json_type() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(sa.JSON(), "mysql")


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


def _create_observation_immutability_triggers() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(
                sa.text(
                    f"CREATE TRIGGER trg_label_observations_no_{action.lower()} "
                    f"BEFORE {action} ON label_observations "
                    "BEGIN SELECT RAISE(ABORT, 'append-only label observation'); END"
                )
            )
    elif dialect in {"mysql", "mariadb"}:
        for action in ("UPDATE", "DELETE"):
            op.execute(
                sa.text(
                    f"CREATE TRIGGER trg_label_observations_no_{action.lower()} "
                    f"BEFORE {action} ON label_observations FOR EACH ROW "
                    "SIGNAL SQLSTATE '45000' "
                    "SET MESSAGE_TEXT = 'append-only label observation'"
                )
            )


def _drop_observation_immutability_triggers() -> None:
    for action in ("update", "delete"):
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS trg_label_observations_no_{action}"))


def upgrade() -> None:
    op.create_table(
        "label_nodes",
        sa.Column("node_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("label_id", sa.String(128), nullable=False),
        sa.Column("canonical_name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("payload", json_type(), nullable=False),
        *timestamp_columns(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "label_id",
            name="uq_label_nodes_scope_label",
        ),
    )
    op.create_index(
        "ix_label_nodes_scope_status",
        "label_nodes",
        ["tenant_id", "project_id", "status"],
    )
    op.create_index("ix_label_nodes_trace_id", "label_nodes", ["trace_id"])

    op.create_table(
        "label_version_items",
        sa.Column("label_version_item_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("label_version_id", sa.String(128), nullable=False),
        sa.Column("label_id", sa.String(128), nullable=False),
        sa.Column("canonical_name", sa.String(255), nullable=False),
        sa.Column("aliases", json_type(), nullable=False),
        sa.Column("value_type", sa.String(32), nullable=False),
        sa.Column("risk_level", sa.String(32), nullable=False),
        sa.Column("mutual_exclusion_group", sa.String(128), nullable=True),
        sa.Column("parent_ids", json_type(), nullable=False),
        sa.Column("aggregation_rule", json_type(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        *timestamp_columns(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "label_version_id",
            "label_id",
            name="uq_label_version_items_scope_label",
        ),
    )
    op.create_index(
        "ix_label_version_items_scope_version",
        "label_version_items",
        ["tenant_id", "project_id", "label_version_id"],
    )
    op.create_index("ix_label_version_items_trace_id", "label_version_items", ["trace_id"])

    op.create_table(
        "label_extraction_runs",
        sa.Column("extraction_run_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("label_version_id", sa.String(128), nullable=False),
        sa.Column("prompt_version_id", sa.String(128), nullable=False),
        sa.Column("model_version", sa.String(128), nullable=False),
        sa.Column("schema_version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("subject_scope", sa.String(64), nullable=False),
        sa.Column("subject_refs", json_type(), nullable=False),
        sa.Column("input_sha256", sa.String(64), nullable=False),
        sa.Column("observation_count", sa.Integer(), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("payload", json_type(), nullable=False),
        *timestamp_columns(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "extraction_run_id",
            name="uq_label_extract_runs_scope",
        ),
    )
    op.create_index(
        "ix_label_extract_runs_scope_status",
        "label_extraction_runs",
        ["tenant_id", "project_id", "status"],
    )
    op.create_index("ix_label_extraction_runs_trace_id", "label_extraction_runs", ["trace_id"])

    op.create_table(
        "label_observations",
        sa.Column("observation_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("extraction_run_id", sa.String(128), nullable=False),
        sa.Column("subject_scope", sa.String(64), nullable=False),
        sa.Column("subject_key", sa.String(256), nullable=False),
        sa.Column("evidence_ref", json_type(), nullable=False),
        sa.Column("evidence_sha256", sa.String(64), nullable=False),
        sa.Column("label_version_id", sa.String(128), nullable=False),
        sa.Column("raw_label", sa.String(255), nullable=False),
        sa.Column("label_id", sa.String(128), nullable=True),
        sa.Column("value_type", sa.String(32), nullable=False),
        sa.Column("value_json", json_type(), nullable=False),
        sa.Column("source_family", sa.String(128), nullable=False),
        sa.Column("source_type", sa.String(64), nullable=False),
        sa.Column("model_version", sa.String(128), nullable=False),
        sa.Column("prompt_version_id", sa.String(128), nullable=False),
        sa.Column("schema_version", sa.String(64), nullable=False),
        sa.Column("calibration_version_id", sa.String(128), nullable=True),
        sa.Column("raw_confidence", sa.Float(), nullable=False),
        sa.Column("calibrated_confidence", sa.Float(), nullable=True),
        sa.Column("input_sha256", sa.String(64), nullable=False),
        sa.Column("output_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("payload", json_type(), nullable=False),
        *timestamp_columns(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "observation_id",
            name="uq_label_observations_scope",
        ),
    )
    op.create_index(
        "ix_label_observations_bucket",
        "label_observations",
        [
            "tenant_id",
            "project_id",
            "label_version_id",
            "subject_scope",
            "subject_key",
        ],
    )
    op.create_index(
        "ix_label_observations_evidence",
        "label_observations",
        ["tenant_id", "project_id", "evidence_sha256", "source_family"],
    )
    op.create_index("ix_label_observations_trace_id", "label_observations", ["trace_id"])
    _create_observation_immutability_triggers()

    op.create_table(
        "label_aggregation_policy_versions",
        sa.Column("policy_version_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("label_version_id", sa.String(128), nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("source_weights", json_type(), nullable=False),
        sa.Column("calibration_versions", json_type(), nullable=False),
        sa.Column("thresholds", json_type(), nullable=False),
        sa.Column("label_definitions", json_type(), nullable=False),
        sa.Column("canonical_sha256", sa.String(64), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("payload", json_type(), nullable=False),
        *timestamp_columns(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "label_version_id",
            "policy_version",
            name="uq_label_agg_policies_scope_version",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "label_version_id",
            "canonical_sha256",
            name="uq_label_agg_policies_scope_hash",
        ),
    )
    op.create_index(
        "ix_label_agg_policies_scope_status",
        "label_aggregation_policy_versions",
        ["tenant_id", "project_id", "status"],
    )
    op.create_index(
        "ix_label_aggregation_policy_versions_trace_id",
        "label_aggregation_policy_versions",
        ["trace_id"],
    )

    op.create_table(
        "label_aggregation_runs",
        sa.Column("aggregation_run_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("label_version_id", sa.String(128), nullable=False),
        sa.Column("policy_version_id", sa.String(128), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("observation_count", sa.Integer(), nullable=False),
        sa.Column("aggregate_count", sa.Integer(), nullable=False),
        sa.Column("input_sha256", sa.String(64), nullable=False),
        sa.Column("result_sha256", sa.String(64), nullable=True),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("payload", json_type(), nullable=False),
        *timestamp_columns(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "aggregation_run_id",
            name="uq_label_agg_runs_scope",
        ),
    )
    op.create_index(
        "ix_label_agg_runs_scope_status",
        "label_aggregation_runs",
        ["tenant_id", "project_id", "status"],
    )
    op.create_index("ix_label_aggregation_runs_trace_id", "label_aggregation_runs", ["trace_id"])

    op.create_table(
        "label_aggregates",
        sa.Column("aggregate_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("aggregation_run_id", sa.String(128), nullable=False),
        sa.Column("label_version_id", sa.String(128), nullable=False),
        sa.Column("policy_version_id", sa.String(128), nullable=False),
        sa.Column("calibration_version_ids", json_type(), nullable=False),
        sa.Column("subject_scope", sa.String(64), nullable=False),
        sa.Column("subject_key", sa.String(256), nullable=False),
        sa.Column("label_id", sa.String(128), nullable=False),
        sa.Column("value_type", sa.String(32), nullable=False),
        sa.Column("value_json", json_type(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("margin", sa.Float(), nullable=True),
        sa.Column("risk_level", sa.String(32), nullable=False),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("reason_codes", json_type(), nullable=False),
        sa.Column("explanation", json_type(), nullable=False),
        sa.Column("bucket_sha256", sa.String(64), nullable=False),
        sa.Column("deterministic_hash", sa.String(64), nullable=False),
        sa.Column("review_task_id", sa.String(128), nullable=True),
        sa.Column("trace_id", sa.String(128), nullable=False),
        *timestamp_columns(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "aggregation_run_id",
            "bucket_sha256",
            name="uq_label_aggregates_run_bucket",
        ),
    )
    op.create_index(
        "ix_label_aggregates_scope_subject",
        "label_aggregates",
        ["tenant_id", "project_id", "subject_scope", "subject_key"],
    )
    op.create_index(
        "ix_label_aggregates_scope_decision",
        "label_aggregates",
        ["tenant_id", "project_id", "decision"],
    )
    op.create_index("ix_label_aggregates_trace_id", "label_aggregates", ["trace_id"])

    op.create_table(
        "label_aggregate_members",
        sa.Column("aggregate_member_id", sa.String(128), primary_key=True),
        sa.Column("aggregate_id", sa.String(128), nullable=False),
        sa.Column("observation_id", sa.String(128), nullable=False),
        sa.Column("included", sa.Boolean(), nullable=False),
        sa.Column("source_family", sa.String(128), nullable=False),
        sa.Column("evidence_sha256", sa.String(64), nullable=False),
        sa.Column("calibrated_confidence", sa.Float(), nullable=True),
        sa.Column("contribution_score", sa.Float(), nullable=True),
        sa.Column("exclusion_reason", sa.String(128), nullable=True),
        sa.Column("explanation", json_type(), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        *timestamp_columns(),
        sa.UniqueConstraint(
            "aggregate_id",
            "observation_id",
            name="uq_label_aggregate_members_pair",
        ),
    )
    op.create_index(
        "ix_label_aggregate_members_aggregate",
        "label_aggregate_members",
        ["aggregate_id", "included"],
    )
    op.create_index("ix_label_aggregate_members_trace_id", "label_aggregate_members", ["trace_id"])

    op.create_table(
        "label_facts",
        sa.Column("fact_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("aggregate_id", sa.String(128), nullable=False),
        sa.Column("supersedes_fact_id", sa.String(128), nullable=True),
        sa.Column("label_version_id", sa.String(128), nullable=False),
        sa.Column("subject_scope", sa.String(64), nullable=False),
        sa.Column("subject_key", sa.String(256), nullable=False),
        sa.Column("label_id", sa.String(128), nullable=False),
        sa.Column("value_type", sa.String(32), nullable=False),
        sa.Column("value_json", json_type(), nullable=False),
        sa.Column("authority", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("review_decision_id", sa.String(128), nullable=True),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("payload", json_type(), nullable=False),
        *timestamp_columns(),
        sa.UniqueConstraint("tenant_id", "project_id", "fact_id", name="uq_label_facts_scope"),
    )
    op.create_index(
        "ix_label_facts_scope_subject",
        "label_facts",
        ["tenant_id", "project_id", "subject_scope", "subject_key", "status"],
    )
    op.create_index("ix_label_facts_trace_id", "label_facts", ["trace_id"])

    op.create_table(
        "feedback_examples",
        sa.Column("feedback_example_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("review_decision_id", sa.String(128), nullable=False),
        sa.Column("review_task_id", sa.String(128), nullable=False),
        sa.Column("target_type", sa.String(64), nullable=False),
        sa.Column("target_id", sa.String(128), nullable=False),
        sa.Column("feedback_type", sa.String(64), nullable=False),
        sa.Column("reason_code", sa.String(128), nullable=True),
        sa.Column("field_diff", json_type(), nullable=False),
        sa.Column("before_json", json_type(), nullable=False),
        sa.Column("after_json", json_type(), nullable=False),
        sa.Column("gold_status", sa.String(32), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        *timestamp_columns(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "review_decision_id",
            "target_type",
            "target_id",
            name="uq_feedback_examples_decision_target",
        ),
    )
    op.create_index(
        "ix_feedback_examples_scope_type",
        "feedback_examples",
        ["tenant_id", "project_id", "feedback_type"],
    )
    op.create_index("ix_feedback_examples_trace_id", "feedback_examples", ["trace_id"])

    op.create_table(
        "label_taxonomy_suggestions",
        sa.Column("suggestion_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("label_version_id", sa.String(128), nullable=False),
        sa.Column("normalized_label", sa.String(255), nullable=False),
        sa.Column("raw_labels", json_type(), nullable=False),
        sa.Column("observation_ids", json_type(), nullable=False),
        sa.Column("proposed_action", sa.String(32), nullable=False),
        sa.Column("canonical_target_label_id", sa.String(128), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("review_task_id", sa.String(128), nullable=True),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("payload", json_type(), nullable=False),
        *timestamp_columns(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "label_version_id",
            "normalized_label",
            "status",
            name="uq_taxonomy_suggestions_open_label",
        ),
    )
    op.create_index(
        "ix_taxonomy_suggestions_scope_status",
        "label_taxonomy_suggestions",
        ["tenant_id", "project_id", "status"],
    )
    op.create_index(
        "ix_label_taxonomy_suggestions_trace_id",
        "label_taxonomy_suggestions",
        ["trace_id"],
    )

    op.create_table(
        "prompt_assets",
        sa.Column("prompt_asset_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("capability", sa.String(64), nullable=False),
        sa.Column("label_version_id", sa.String(128), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("current_version_id", sa.String(128), nullable=True),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("payload", json_type(), nullable=False),
        *timestamp_columns(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "prompt_asset_id",
            name="uq_prompt_assets_scope",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "capability",
            "name",
            name="uq_prompt_assets_name",
        ),
    )
    op.create_index("ix_prompt_assets_trace_id", "prompt_assets", ["trace_id"])

    op.create_table(
        "prompt_versions",
        sa.Column("prompt_version_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("prompt_asset_id", sa.String(128), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("parent_version_id", sa.String(128), nullable=True),
        sa.Column("label_version_id", sa.String(128), nullable=True),
        sa.Column("schema_version", sa.String(64), nullable=False),
        sa.Column("model_version", sa.String(128), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("template_json", json_type(), nullable=False),
        sa.Column("output_schema", json_type(), nullable=False),
        sa.Column("generation_params", json_type(), nullable=False),
        sa.Column("structured_diff", json_type(), nullable=False),
        sa.Column("source_badcase_refs", json_type(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        *timestamp_columns(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "prompt_asset_id",
            "version",
            name="uq_prompt_versions_asset_version",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "content_sha256",
            name="uq_prompt_versions_scope_hash",
        ),
    )
    op.create_index(
        "ix_prompt_versions_scope_status",
        "prompt_versions",
        ["tenant_id", "project_id", "status"],
    )
    op.create_index("ix_prompt_versions_trace_id", "prompt_versions", ["trace_id"])

    op.create_table(
        "release_deployments",
        sa.Column("deployment_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("environment", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("label_version_id", sa.String(128), nullable=False),
        sa.Column("prompt_version_id", sa.String(128), nullable=False),
        sa.Column("model_version", sa.String(128), nullable=False),
        sa.Column("aggregation_policy_version_id", sa.String(128), nullable=False),
        sa.Column("eval_dataset_version_id", sa.String(128), nullable=False),
        sa.Column("eval_run_id", sa.String(128), nullable=False),
        sa.Column("rollback_target_deployment_id", sa.String(128), nullable=True),
        sa.Column("bundle_sha256", sa.String(64), nullable=False),
        sa.Column("rollout_percentage", sa.Integer(), nullable=False),
        sa.Column("blocked_reasons", json_type(), nullable=False),
        sa.Column("monitor_metrics", json_type(), nullable=False),
        sa.Column("approved_by", sa.String(64), nullable=True),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("payload", json_type(), nullable=False),
        *timestamp_columns(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "deployment_id",
            name="uq_release_deployments_scope",
        ),
    )
    op.create_index(
        "ix_release_deployments_scope_status",
        "release_deployments",
        ["tenant_id", "project_id", "status"],
    )
    op.create_index("ix_release_deployments_trace_id", "release_deployments", ["trace_id"])


def downgrade() -> None:
    op.drop_table("release_deployments")
    op.drop_table("prompt_versions")
    op.drop_table("prompt_assets")
    op.drop_table("label_taxonomy_suggestions")
    op.drop_table("feedback_examples")
    op.drop_table("label_facts")
    op.drop_table("label_aggregate_members")
    op.drop_table("label_aggregates")
    op.drop_table("label_aggregation_runs")
    op.drop_table("label_aggregation_policy_versions")
    _drop_observation_immutability_triggers()
    op.drop_table("label_observations")
    op.drop_table("label_extraction_runs")
    op.drop_table("label_version_items")
    op.drop_table("label_nodes")
