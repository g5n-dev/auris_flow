from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Boolean, Integer, create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.sql.elements import TextClause

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"


CORE_TABLES = {
    "auth_sessions",
    "browser_auth_sessions",
    "tenants",
    "projects",
    "users",
    "json_resources",
    "run_records",
    "user_security_states",
    "idempotency_records",
    "oidc_authorization_states",
    "oidc_identities",
    "audit_logs",
    "outbox_delivery_attempts",
    "outbox_events",
}

DOMAIN_BASELINE_TABLES = {
    "asset_materializations",
    "asset_lineage_edges",
    "asset_partitions",
    "audio_recordings",
    "audio_sessions",
    "badcases",
    "conversation_boundaries",
    "controlled_experiments",
    "data_assets",
    "eval_cases",
    "eval_datasets",
    "eval_runs",
    "evidence_packs",
    "experiment_assignments",
    "experiment_decisions",
    "experiment_exposures",
    "experiment_metric_snapshots",
    "experiment_outcomes",
    "external_callback_receipts",
    "human_review_decisions",
    "human_review_tasks",
    "insight_actions",
    "insight_effects",
    "insight_experiments",
    "insight_reports",
    "knowledge_chunks",
    "knowledge_effects",
    "knowledge_indexes",
    "knowledge_quality_gates",
    "knowledge_sources",
    "label_candidates",
    "label_conflicts",
    "label_policy_evaluations",
    "label_policy_versions",
    "label_versions",
    "metric_results",
    "quality_appeals",
    "project_scene_profile_bindings",
    "scene_profile_versions",
    "scene_profiles",
    "storage_objects",
    "task_run_steps",
    "task_version_release_heads",
    "task_versions",
}

OUTBOX_LEASE_COLUMNS = {
    "dispatch_idempotency_key",
    "dispatch_request_sha256",
    "claim_token",
    "claimed_by",
    "claimed_at",
    "lease_generation",
    "lease_expires_at",
}

OUTBOX_LEASE_INDEXES = {
    "ix_outbox_events_claim",
    "ix_outbox_events_reclaim",
    "ix_outbox_events_scope_aggregate",
    "uq_outbox_events_dispatch_idempotency_key",
}

TASK_RUN_CONTROL_COLUMNS = {
    "submitted_at",
    "started_at",
    "finished_at",
    "deadline_at",
    "next_status_sync_at",
    "monitor_generation",
    "engine_status",
    "engine_status_observed_at",
    "status_version",
    "cancel_requested_at",
    "cancel_reason",
    "terminal_reason",
}

TASK_RUN_CONTROL_INDEXES = {
    "ix_run_records_engine_status",
    "ix_run_records_monitor_control_active",
    "ix_run_records_monitor_deadline",
    "ix_run_records_monitor_sync_due",
    "ix_run_records_status_deadline",
    "ix_run_records_status_sync_due",
    "ix_run_records_type_status_finished",
}

TASK_RUN_CONTROL_INDEX_COLUMNS = {
    "ix_run_records_monitor_control_active": [
        "tenant_id",
        "project_id",
        "run_key",
        "run_type",
        "status",
    ],
    "ix_run_records_monitor_deadline": ["run_type", "status", "deadline_at"],
    "ix_run_records_monitor_sync_due": [
        "run_type",
        "status",
        "next_status_sync_at",
    ],
}

LABEL_VERSION_POLICY_COLUMNS = {
    "resource_version",
    "policy_version_id",
    "release_gate_id",
}

LABEL_LIFECYCLE_TABLES = {
    "insight_report_metric_bindings",
    "label_taxonomies",
    "metric_result_label_scopes",
    "label_mapping_versions",
    "label_mapping_items",
    "label_mapping_item_targets",
    "label_mapping_bundles",
    "label_mapping_bundle_sources",
    "label_mapping_bundle_members",
    "label_mapping_bundle_paths",
    "release_bundle_head_events",
}

LABEL_LIFECYCLE_COLUMNS = {
    "metric_results": {
        "content_sha256",
        "source_manifest_sha256",
        "scope_sha256",
        "root_trace_id",
        "action_trace_id",
    },
    "metric_result_label_scopes": {
        "metric_scope_id",
        "metric_result_id",
        "taxonomy_mode",
        "source_label_version_ids",
        "target_label_version_id",
        "mapping_bundle_id",
        "mapping_bundle_sha256",
        "fact_namespace",
        "fact_set_id",
        "fact_set_manifest_sha256",
        "fact_set_generation",
        "fact_as_of",
        "metric_definition_versions",
        "timezone",
        "period_boundary",
        "denominator_definition",
        "label_version_applicability",
        "comparability_status",
        "comparability_reason_codes",
        "scope_sha256",
        "source_manifest_sha256",
        "content_sha256",
        "root_trace_id",
        "action_trace_id",
        "trace_id",
        "payload",
        "created_at",
    },
    "insight_report_metric_bindings": {
        "report_metric_binding_id",
        "report_id",
        "metric_result_ids",
        "result_count",
        "metric_scope_sha256",
        "content_sha256",
        "root_trace_id",
        "action_trace_id",
        "trace_id",
        "payload",
        "created_at",
    },
    "label_versions": {
        "taxonomy_id",
        "semantic_version",
        "base_label_version_id",
        "artifact_status",
        "artifact_published_at",
        "artifact_deprecated_at",
        "deprecation_reason",
        "replacement_label_version_id",
        "content_sha256",
    },
    "label_version_items": {"definition_sha256"},
    "label_mapping_versions": {
        "mapping_version_id",
        "source_label_version_id",
        "target_label_version_id",
        "mapping_version",
        "source_resource_version",
        "target_resource_version",
        "content_sha256",
    },
    "label_mapping_items": {
        "mapping_item_id",
        "mapping_version_id",
        "source_label_id",
        "relation",
        "compatibility",
        "comparability_status",
        "requires_recompute",
        "content_sha256",
    },
    "label_mapping_item_targets": {
        "mapping_item_target_id",
        "mapping_item_id",
        "target_label_version_id",
        "target_label_id",
        "target_order",
    },
    "label_mapping_bundles": {
        "mapping_bundle_id",
        "source_label_version_ids",
        "target_label_version_id",
        "compiler_version",
        "canonical_manifest_sha256",
    },
    "label_mapping_bundle_sources": {
        "mapping_bundle_id",
        "source_label_version_id",
        "source_resource_version",
        "source_order",
    },
    "label_mapping_bundle_members": {
        "mapping_bundle_id",
        "mapping_version_id",
        "edge_order",
        "edge_content_sha256",
    },
    "label_mapping_bundle_paths": {
        "mapping_bundle_id",
        "source_label_version_id",
        "target_label_version_id",
        "source_label_id",
        "target_label_id",
        "metric_family",
        "path_sha256",
    },
    "release_bundle_head_events": {
        "environment",
        "generation",
        "previous_generation",
        "action",
        "activation_status",
        "old_deployment_id",
        "new_deployment_id",
        "old_label_version_id",
        "new_label_version_id",
        "effective_from",
        "effective_to",
        "command_id",
        "completion_receipt_id",
        "content_sha256",
    },
}

LABEL_LIFECYCLE_UNIQUES = {
    "metric_result_label_scopes": {
        "uq_metric_result_label_scopes_scope_result",
        "uq_metric_result_label_scopes_scope_hash",
    },
    "insight_report_metric_bindings": {
        "uq_insight_report_metric_bindings_scope_report",
        "uq_insight_report_metric_bindings_scope_hash",
    },
    "label_taxonomies": {"uq_label_taxonomies_scope_id", "uq_label_taxonomies_scope_name"},
    "label_versions": {
        "uq_label_versions_scope_taxonomy_semver",
        "uq_label_versions_scope_taxonomy_id",
    },
    "label_mapping_versions": {
        "uq_label_mapping_versions_scope_id",
        "uq_label_mapping_versions_scope_version",
        "uq_label_mapping_versions_scope_hash",
        "uq_label_mapping_versions_scope_edge_binding",
    },
    "label_mapping_items": {"uq_label_mapping_items_source_disposition"},
    "label_mapping_item_targets": {
        "uq_label_mapping_item_targets_label",
        "uq_label_mapping_item_targets_order",
    },
    "label_mapping_bundles": {
        "uq_label_mapping_bundles_scope_id",
        "uq_label_mapping_bundles_scope_hash",
        "uq_label_mapping_bundles_scope_target_id",
    },
    "release_bundle_head_events": {
        "uq_release_bundle_head_events_generation",
        "uq_release_bundle_head_events_hash",
    },
}

LABEL_LIFECYCLE_CHECKS = {
    "metric_result_label_scopes": {
        "ck_metric_result_label_scopes_mode",
        "ck_metric_result_label_scopes_mode_binding",
        "ck_metric_result_label_scopes_generation",
        "ck_metric_result_label_scopes_applicability",
        "ck_metric_result_label_scopes_comparability",
        "ck_metric_result_label_scopes_hashes",
    },
    "insight_report_metric_bindings": {
        "ck_insight_report_metric_bindings_hashes",
        "ck_insight_report_metric_bindings_result_count",
    },
    "label_versions": {"ck_label_versions_artifact_status"},
    "label_version_items": {"ck_label_version_items_status"},
    "label_mapping_versions": {
        "ck_label_mapping_versions_distinct_pair",
        "ck_label_mapping_versions_status",
        "ck_label_mapping_versions_resource_versions",
    },
    "label_mapping_items": {
        "ck_label_mapping_items_relation",
        "ck_label_mapping_items_compatibility",
        "ck_label_mapping_items_comparability",
        "ck_label_mapping_items_split_recompute",
    },
    "label_mapping_bundles": {
        "ck_label_mapping_bundles_status",
        "ck_label_mapping_bundles_resource_version",
    },
    "release_bundle_head_events": {
        "ck_release_bundle_head_events_generation",
        "ck_release_bundle_head_events_previous_generation",
        "ck_release_bundle_head_events_action",
        "ck_release_bundle_head_events_status",
        "ck_release_bundle_head_events_interval",
    },
}

LABEL_LIFECYCLE_INDEXES = {
    "metric_result_label_scopes": {
        "ix_metric_result_label_scopes_scope_target",
        "ix_metric_result_label_scopes_scope_fact_cutoff",
        "ix_metric_result_label_scopes_trace_id",
    },
    "insight_report_metric_bindings": {
        "ix_insight_report_metric_bindings_trace_id",
    },
    "label_versions": {
        "ix_label_versions_scope_artifact_status",
        "ix_label_versions_scope_taxonomy",
        "ix_label_versions_scope_replacement",
    },
    "label_version_items": {"ix_label_version_items_scope_status"},
    "label_mapping_versions": {
        "ix_label_mapping_versions_scope_status",
        "ix_label_mapping_versions_scope_pair",
    },
    "label_mapping_bundles": {
        "ix_label_mapping_bundles_scope_status",
        "ix_label_mapping_bundles_scope_target",
        "uq_label_mapping_bundles_scope_id_hash",
    },
    "label_fact_sets": {"uq_label_fact_sets_scope_metric_binding"},
    "label_mapping_bundle_paths": {"ix_label_mapping_bundle_paths_scope_target"},
    "release_bundle_head_events": {
        "ix_release_bundle_head_events_scope_timeline",
        "ix_release_bundle_head_events_scope_label",
    },
}

LABEL_LIFECYCLE_FOREIGN_KEYS = {
    "metric_result_label_scopes": {
        "fk_metric_result_label_scopes_scope_result": "metric_results",
        "fk_metric_result_label_scopes_scope_target_version": "label_versions",
        "fk_metric_result_label_scopes_scope_mapping_bundle": "label_mapping_bundles",
        "fk_metric_result_label_scopes_scope_fact_set": "label_fact_sets",
    },
    "insight_report_metric_bindings": {
        "fk_insight_report_metric_bindings_scope_report": "insight_reports",
    },
    "label_versions": {
        "fk_label_versions_scope_taxonomy": "label_taxonomies",
        "fk_label_versions_scope_base": "label_versions",
        "fk_label_versions_scope_replacement": "label_versions",
    },
    "label_mapping_versions": {
        "fk_label_mapping_versions_scope_source": "label_versions",
        "fk_label_mapping_versions_scope_target": "label_versions",
    },
    "label_mapping_items": {
        "fk_label_mapping_items_scope_version_pair": "label_mapping_versions",
        "fk_label_mapping_items_scope_source_item": "label_version_items",
    },
    "label_mapping_item_targets": {
        "fk_label_mapping_item_targets_scope_item": "label_mapping_items",
        "fk_label_mapping_item_targets_scope_target": "label_version_items",
    },
    "label_mapping_bundles": {
        "fk_label_mapping_bundles_scope_target": "label_versions",
    },
    "label_mapping_bundle_sources": {
        "fk_label_mapping_bundle_sources_scope_bundle": "label_mapping_bundles",
        "fk_label_mapping_bundle_sources_scope_version": "label_versions",
    },
    "label_mapping_bundle_members": {
        "fk_label_mapping_bundle_members_scope_bundle": "label_mapping_bundles",
        "fk_label_mapping_bundle_members_scope_edge": "label_mapping_versions",
    },
    "label_mapping_bundle_paths": {
        "fk_label_mapping_bundle_paths_scope_bundle_target": "label_mapping_bundles",
        "fk_label_mapping_bundle_paths_scope_source": "label_version_items",
        "fk_label_mapping_bundle_paths_scope_target_item": "label_version_items",
    },
    "release_bundle_head_events": {
        "fk_release_head_events_scope_old_deployment": "release_deployments",
        "fk_release_head_events_scope_new_deployment": "release_deployments",
        "fk_release_head_events_scope_old_label": "label_versions",
        "fk_release_head_events_scope_new_label": "label_versions",
        "fk_release_head_events_scope_command": "release_commands",
        "fk_release_head_events_scope_receipt": "run_completion_receipts",
    },
}

OUTBOX_ATTEMPT_INDEXES = {
    "ix_outbox_delivery_attempts_scope_event",
    "ix_outbox_delivery_attempts_status_started",
}

INSIGHT_UNIQUE_CONSTRAINTS = {
    "insight_reports": "uq_insight_reports_scope_run",
    "insight_experiments": "uq_insight_experiments_scope_run",
    "insight_effects": "uq_insight_effects_scope_experiment",
}

INSIGHT_SCOPE_UNIQUE_CONSTRAINTS = {
    "run_records": (
        "uq_run_records_scope_id",
        ["tenant_id", "project_id", "run_id"],
    ),
    "metric_results": (
        "uq_metric_results_scope",
        ["tenant_id", "project_id", "metric_result_id"],
    ),
    "insight_reports": (
        "uq_insight_reports_scope_id",
        ["tenant_id", "project_id", "report_id"],
    ),
    "insight_actions": (
        "uq_insight_actions_scope_id",
        ["tenant_id", "project_id", "action_id"],
    ),
    "insight_experiments": (
        "uq_insight_experiments_scope_id",
        ["tenant_id", "project_id", "experiment_id"],
    ),
    "insight_effects": (
        "uq_insight_effects_scope_id",
        ["tenant_id", "project_id", "effect_id"],
    ),
}

INSIGHT_CAUSAL_FOREIGN_KEYS = {
    "insight_reports": {
        "fk_insight_reports_scope_run": (
            "run_records",
            ["tenant_id", "project_id", "run_id"],
            ["tenant_id", "project_id", "run_id"],
        ),
    },
    "insight_actions": {
        "fk_insight_actions_scope_report": (
            "insight_reports",
            ["tenant_id", "project_id", "report_id"],
            ["tenant_id", "project_id", "report_id"],
        ),
        "fk_insight_actions_scope_baseline_metric": (
            "metric_results",
            ["tenant_id", "project_id", "baseline_metric_result_id"],
            ["tenant_id", "project_id", "metric_result_id"],
        ),
    },
    "insight_experiments": {
        "fk_insight_experiments_scope_action": (
            "insight_actions",
            ["tenant_id", "project_id", "action_id"],
            ["tenant_id", "project_id", "action_id"],
        ),
        "fk_insight_experiments_scope_run": (
            "run_records",
            ["tenant_id", "project_id", "eval_run_id"],
            ["tenant_id", "project_id", "run_id"],
        ),
        "fk_insight_experiments_scope_baseline_metric": (
            "metric_results",
            ["tenant_id", "project_id", "baseline_metric_result_id"],
            ["tenant_id", "project_id", "metric_result_id"],
        ),
        "fk_insight_experiments_scope_outcome_metric": (
            "metric_results",
            ["tenant_id", "project_id", "outcome_metric_result_id"],
            ["tenant_id", "project_id", "metric_result_id"],
        ),
    },
    "insight_effects": {
        "fk_insight_effects_scope_action": (
            "insight_actions",
            ["tenant_id", "project_id", "action_id"],
            ["tenant_id", "project_id", "action_id"],
        ),
        "fk_insight_effects_scope_experiment": (
            "insight_experiments",
            ["tenant_id", "project_id", "experiment_id"],
            ["tenant_id", "project_id", "experiment_id"],
        ),
        "fk_insight_effects_scope_baseline_metric": (
            "metric_results",
            ["tenant_id", "project_id", "baseline_metric_result_id"],
            ["tenant_id", "project_id", "metric_result_id"],
        ),
        "fk_insight_effects_scope_outcome_metric": (
            "metric_results",
            ["tenant_id", "project_id", "outcome_metric_result_id"],
            ["tenant_id", "project_id", "metric_result_id"],
        ),
    },
}

OIDC_BROWSER_SESSION_COLUMNS = {
    "browser_session_id",
    "token_sha256",
    "csrf_sha256",
    "oidc_identity_id",
    "user_id",
    "tenant_id",
    "project_id",
    "provider",
    "issued_at",
    "expires_at",
    "revoked_at",
    "last_seen_at",
}

INSIGHT_CAUSAL_INDEXES = {
    "insight_actions": {"ix_insight_actions_scope_baseline_metric"},
    "insight_experiments": {
        "ix_insight_experiments_scope_action",
        "ix_insight_experiments_scope_baseline_metric",
        "ix_insight_experiments_scope_outcome_metric",
    },
    "insight_effects": {
        "ix_insight_effects_scope_baseline_metric",
        "ix_insight_effects_scope_outcome_metric",
    },
}

HUMAN_REVIEW_DECISION_COLUMNS = {
    "review_task_id",
    "terminal_review_task_id",
}

QUALITY_APPEAL_COLUMNS = {
    "appeal_id",
    "tenant_id",
    "project_id",
    "source_decision_id",
    "source_review_task_id",
    "review_task_id",
    "appeal_decision_id",
    "source_result_sha256",
    "source_decider_id",
    "source_trace_id",
    "root_trace_id",
    "current_trace_id",
    "appellant_id",
    "evidence_refs",
    "reason",
    "status",
    "reviewer_id",
    "decision",
    "decision_reason",
    "withdrawal_reason",
    "resource_version",
    "claimed_at",
    "resolved_at",
    "withdrawn_at",
    "created_at",
    "updated_at",
}

QUALITY_APPEAL_INDEXES = {
    "ix_quality_appeals_scope_status",
    "ix_quality_appeals_scope_appellant",
    "ix_quality_appeals_scope_reviewer",
}

QUALITY_APPEAL_CHECKS = {
    "ck_quality_appeals_status",
    "ck_quality_appeals_decision",
    "ck_quality_appeals_resolution_state",
    "ck_quality_appeals_withdrawal_state",
}

HOTWORD_TABLES = {
    "asr_annotation_corrections",
    "hotword_packs",
    "hotword_pack_versions",
    "hotword_version_items",
    "hotword_metric_snapshots",
}

LABEL_EVAL_TABLES = {"label_eval_results", "label_eval_suite_results"}

LABEL_EVAL_COLUMNS = {
    "label_eval_results": {
        "eval_result_id",
        "tenant_id",
        "project_id",
        "eval_run_id",
        "status",
        "binding_sha256",
        "dataset_snapshot_sha256",
        "sample_manifest_sha256",
        "result_sha256",
        "overall_metrics",
        "bootstrap_ci",
        "gate_results",
        "trace_id",
        "payload",
        "created_at",
        "updated_at",
    },
    "label_eval_suite_results": {
        "suite_result_id",
        "tenant_id",
        "project_id",
        "eval_result_id",
        "suite",
        "sample_count",
        "sample_manifest_sha256",
        "metrics",
        "suite_sha256",
        "trace_id",
        "created_at",
        "updated_at",
    },
}

LABEL_EVAL_INDEXES = {
    "label_eval_results": {
        "ix_label_eval_results_scope_status",
        "ix_label_eval_results_trace_id",
    },
    "label_eval_suite_results": {
        "ix_label_eval_suite_results_scope_result",
        "ix_label_eval_suite_results_trace_id",
    },
}

LABEL_EVAL_UNIQUES = {
    "label_eval_results": {"uq_label_eval_results_scope_run"},
    "label_eval_suite_results": {"uq_label_eval_suite_results_scope_suite"},
}

LABEL_EVAL_CHECKS = {
    "label_eval_results": {"ck_label_eval_results_status"},
    "label_eval_suite_results": {
        "ck_label_eval_suite_results_sample_count",
        "ck_label_eval_suite_results_suite",
    },
}

HOTWORD_COLUMNS: dict[str, set[str]] = {
    "asr_annotation_corrections": {
        "correction_id",
        "annotation_id",
        "tenant_id",
        "project_id",
        "audio_session_id",
        "observed_at",
        "status",
        "standard_term",
        "normalized_term",
        "recognized_text",
        "corrected_text",
        "error_type",
        "evidence_storage_object_id",
        "evidence_window",
        "hotword_pack_version_id",
        "source_badcase_id",
        "store_id",
        "provider",
        "model_version",
        "evidence_level",
        "correction_fingerprint",
        "semantic_sha256",
        "root_trace_id",
        "source_trace_id",
        "current_trace_id",
        "payload",
        "created_at",
        "updated_at",
    },
    "hotword_packs": {
        "pack_id",
        "tenant_id",
        "project_id",
        "name",
        "language",
        "domain",
        "status",
        "current_version_id",
        "production_version_id",
        "resource_version",
        "root_trace_id",
        "current_trace_id",
        "created_at",
        "updated_at",
    },
    "hotword_pack_versions": {
        "version_id",
        "tenant_id",
        "project_id",
        "pack_id",
        "version",
        "baseline_version_id",
        "status",
        "content_sha256",
        "manifest_storage_object_id",
        "eval_run_id",
        "eval_locked",
        "model_approved_by",
        "project_admin_confirmed_by",
        "provider_artifact_ref",
        "compiled_provider",
        "resource_version",
        "root_trace_id",
        "current_trace_id",
        "published_at",
        "payload",
        "created_at",
        "updated_at",
    },
    "hotword_version_items": {
        "item_id",
        "tenant_id",
        "project_id",
        "version_id",
        "canonical_term",
        "normalized_term",
        "aliases",
        "category",
        "weight",
        "source_badcase_id",
        "source_type",
        "resource_version",
        "root_trace_id",
        "current_trace_id",
        "created_at",
        "updated_at",
    },
    "hotword_metric_snapshots": {
        "snapshot_id",
        "tenant_id",
        "project_id",
        "bucket_start",
        "bucket_end",
        "store_id",
        "provider",
        "model_version",
        "hotword_pack_version_id",
        "standard_term",
        "expected_count",
        "correct_count",
        "weighted_error_count",
        "false_insert_count",
        "recognized_hotword_count",
        "impacted_session_count",
        "evidence_confidence",
        "root_trace_id",
        "payload",
        "created_at",
        "updated_at",
    },
}

HOTWORD_NON_NULL_COLUMNS: dict[str, set[str]] = {
    "asr_annotation_corrections": HOTWORD_COLUMNS["asr_annotation_corrections"]
    - {"store_id", "provider", "model_version"},
    "hotword_packs": HOTWORD_COLUMNS["hotword_packs"]
    - {"current_version_id", "production_version_id"},
    "hotword_pack_versions": {
        "version_id",
        "tenant_id",
        "project_id",
        "pack_id",
        "version",
        "status",
        "eval_locked",
        "resource_version",
        "root_trace_id",
        "current_trace_id",
        "payload",
        "created_at",
        "updated_at",
    },
    "hotword_version_items": HOTWORD_COLUMNS["hotword_version_items"] - {"source_badcase_id"},
    "hotword_metric_snapshots": HOTWORD_COLUMNS["hotword_metric_snapshots"]
    - {
        "store_id",
        "provider",
        "model_version",
        "hotword_pack_version_id",
        "standard_term",
    },
}

HOTWORD_PRIMARY_KEYS: dict[str, list[str]] = {
    "asr_annotation_corrections": ["correction_id"],
    "hotword_packs": ["pack_id"],
    "hotword_pack_versions": ["version_id"],
    "hotword_version_items": ["item_id"],
    "hotword_metric_snapshots": ["snapshot_id"],
}

HOTWORD_UNIQUE_CONSTRAINTS: dict[str, dict[str, list[str]]] = {
    "asr_annotation_corrections": {
        "uq_asr_annotation_corrections_scope_id": [
            "tenant_id",
            "project_id",
            "correction_id",
        ],
        "uq_asr_annotation_corrections_scope_annotation": [
            "tenant_id",
            "project_id",
            "annotation_id",
        ],
        "uq_asr_annotation_corrections_scope_fingerprint": [
            "tenant_id",
            "project_id",
            "correction_fingerprint",
        ],
        "uq_asr_annotation_corrections_scope_evidence": [
            "tenant_id",
            "project_id",
            "evidence_storage_object_id",
        ],
    },
    "hotword_packs": {
        "uq_hotword_packs_scope_id": ["tenant_id", "project_id", "pack_id"],
        "uq_hotword_packs_scope_name": [
            "tenant_id",
            "project_id",
            "name",
            "language",
            "domain",
        ],
    },
    "hotword_pack_versions": {
        "uq_hotword_pack_versions_scope_id": ["tenant_id", "project_id", "version_id"],
        "uq_hotword_pack_versions_scope_version": [
            "tenant_id",
            "project_id",
            "pack_id",
            "version",
        ],
    },
    "hotword_version_items": {
        "uq_hotword_version_items_scope_id": ["tenant_id", "project_id", "item_id"],
        "uq_hotword_version_items_scope_term": [
            "tenant_id",
            "project_id",
            "version_id",
            "normalized_term",
        ],
    },
    "hotword_metric_snapshots": {
        "uq_hotword_metric_snapshots_scope_id": [
            "tenant_id",
            "project_id",
            "snapshot_id",
        ],
    },
    "badcases": {
        "uq_badcases_scope": ["tenant_id", "project_id", "badcase_id"],
    },
}

HOTWORD_FOREIGN_KEYS: dict[
    str,
    dict[str, tuple[str, list[str], list[str]]],
] = {
    "asr_annotation_corrections": {
        "fk_asr_annotation_corrections_scope_version": (
            "hotword_pack_versions",
            ["tenant_id", "project_id", "hotword_pack_version_id"],
            ["tenant_id", "project_id", "version_id"],
        ),
        "fk_asr_annotation_corrections_scope_badcase": (
            "badcases",
            ["tenant_id", "project_id", "source_badcase_id"],
            ["tenant_id", "project_id", "badcase_id"],
        ),
    },
    "hotword_pack_versions": {
        "fk_hotword_pack_versions_scope_pack": (
            "hotword_packs",
            ["tenant_id", "project_id", "pack_id"],
            ["tenant_id", "project_id", "pack_id"],
        ),
        "fk_hotword_pack_versions_scope_baseline": (
            "hotword_pack_versions",
            ["tenant_id", "project_id", "baseline_version_id"],
            ["tenant_id", "project_id", "version_id"],
        ),
    },
    "hotword_version_items": {
        "fk_hotword_version_items_scope_version": (
            "hotword_pack_versions",
            ["tenant_id", "project_id", "version_id"],
            ["tenant_id", "project_id", "version_id"],
        ),
        "fk_hotword_version_items_scope_badcase": (
            "badcases",
            ["tenant_id", "project_id", "source_badcase_id"],
            ["tenant_id", "project_id", "badcase_id"],
        ),
    },
    "hotword_metric_snapshots": {
        "fk_hotword_metric_snapshots_scope_version": (
            "hotword_pack_versions",
            ["tenant_id", "project_id", "hotword_pack_version_id"],
            ["tenant_id", "project_id", "version_id"],
        ),
    },
    "badcases": {
        "fk_badcases_scope_hotword_version": (
            "hotword_pack_versions",
            ["tenant_id", "project_id", "hotword_pack_version_id"],
            ["tenant_id", "project_id", "version_id"],
        ),
    },
}

HOTWORD_CHECK_FRAGMENTS: dict[str, dict[str, tuple[str, ...]]] = {
    "asr_annotation_corrections": {
        "ck_asr_annotation_corrections_status": ("status", "submitted"),
        "ck_asr_annotation_corrections_evidence_level": (
            "evidence_level",
            "discovery",
        ),
        "ck_asr_annotation_corrections_error_type": ("error_type", "in"),
    },
    "hotword_packs": {
        "ck_hotword_packs_resource_version": ("resource_version>0",),
    },
    "hotword_pack_versions": {
        "ck_hotword_pack_versions_resource_version": ("resource_version>0",),
        "ck_hotword_pack_versions_status": ("status", "in"),
    },
    "hotword_version_items": {
        "ck_hotword_version_items_weight": ("weightbetween0and100",),
        "ck_hotword_version_items_resource_version": ("resource_version>0",),
        "ck_hotword_version_items_source_type": ("source_type", "in"),
    },
    "hotword_metric_snapshots": {
        "ck_hotword_metric_snapshots_counts": (
            "expected_count>=0",
            "correct_count>=0",
            "weighted_error_count>=0",
            "false_insert_count>=0",
            "recognized_hotword_count>=0",
            "impacted_session_count>=0",
            "correct_count<=expected_count",
        ),
        "ck_hotword_metric_snapshots_evidence_confidence": ("evidence_confidencebetween0and1",),
        "ck_hotword_metric_snapshots_bucket": ("bucket_end>bucket_start",),
    },
    "badcases": {
        "ck_badcases_resource_version": ("resource_version>0",),
        "ck_badcases_hotword_error_type": ("error_typeisnull", "error_type", "in"),
    },
}

HOTWORD_CHECK_LITERALS: dict[str, set[str]] = {
    "ck_asr_annotation_corrections_status": {"submitted"},
    "ck_asr_annotation_corrections_evidence_level": {"discovery"},
    "ck_asr_annotation_corrections_error_type": {
        "missing_term",
        "misrecognition",
        "alias_gap",
        "weight_issue",
        "false_boost",
    },
    "ck_hotword_pack_versions_status": {
        "draft",
        "validating",
        "ready_for_eval",
        "evaluating",
        "gate_blocked",
        "review_required",
        "approved",
        "published",
        "deprecated",
        "rolled_back",
        "archived",
    },
    "ck_hotword_version_items_source_type": {
        "manual",
        "badcase",
        "knowledge_candidate",
    },
    "ck_badcases_hotword_error_type": {
        "missing_term",
        "misrecognition",
        "alias_gap",
        "weight_issue",
        "false_boost",
    },
}

HOTWORD_INDEXES: dict[str, dict[str, list[str]]] = {
    "asr_annotation_corrections": {
        "ix_asr_annotation_corrections_scope_observed": [
            "tenant_id",
            "project_id",
            "observed_at",
        ],
        "ix_asr_annotation_corrections_scope_dimensions": [
            "tenant_id",
            "project_id",
            "store_id",
            "provider",
            "model_version",
            "hotword_pack_version_id",
        ],
        "ix_asr_annotation_corrections_scope_term": [
            "tenant_id",
            "project_id",
            "normalized_term",
        ],
    },
    "hotword_packs": {
        "ix_hotword_packs_scope_status": ["tenant_id", "project_id", "status"],
        "ix_hotword_packs_scope_production_version": [
            "tenant_id",
            "project_id",
            "production_version_id",
        ],
    },
    "hotword_pack_versions": {
        "ix_hotword_pack_versions_scope_status": ["tenant_id", "project_id", "status"],
        "ix_hotword_pack_versions_scope_pack": ["tenant_id", "project_id", "pack_id"],
    },
    "hotword_version_items": {
        "ix_hotword_version_items_scope_version": [
            "tenant_id",
            "project_id",
            "version_id",
        ],
    },
    "hotword_metric_snapshots": {
        "ix_hotword_metric_snapshots_scope_bucket": [
            "tenant_id",
            "project_id",
            "bucket_start",
            "bucket_end",
        ],
        "ix_hotword_metric_snapshots_scope_dimensions": [
            "tenant_id",
            "project_id",
            "store_id",
            "provider",
            "model_version",
            "hotword_pack_version_id",
        ],
    },
    "badcases": {
        "ix_badcases_scope_capability_status": [
            "tenant_id",
            "project_id",
            "capability",
            "status",
        ],
        "ix_badcases_scope_hotword_version": [
            "tenant_id",
            "project_id",
            "hotword_pack_version_id",
        ],
    },
}

HOTWORD_BADCASE_COLUMNS = {
    "capability",
    "error_type",
    "standard_term",
    "recognized_text",
    "evidence_ref",
    "evidence_storage_object_id",
    "evidence_level",
    "hotword_pack_version_id",
    "expected_count",
    "correct_count",
    "weighted_error_count",
    "manual_correction_count",
    "priority_score",
    "candidate_state",
    "root_cause",
    "fix_suggestion",
    "downstream_impact",
    "resource_version",
    "root_trace_id",
    "current_trace_id",
}

HOTWORD_BADCASE_NON_NULL_COLUMNS = {
    "expected_count",
    "correct_count",
    "weighted_error_count",
    "manual_correction_count",
    "priority_score",
    "candidate_state",
    "downstream_impact",
    "resource_version",
    "root_trace_id",
    "current_trace_id",
}

CALIBRATION_TABLES = {
    "calibration_adjudications",
    "calibration_assignments",
    "calibration_items",
    "calibration_rounds",
    "calibration_submissions",
    "gold_annotations",
    "gold_set_series",
    "gold_set_versions",
}

CALIBRATION_UNIQUE_CONSTRAINTS = {
    "calibration_rounds": {
        "uq_cal_rounds_scope_id": ["tenant_id", "project_id", "round_id"],
    },
    "calibration_items": {
        "uq_cal_items_scope_id": ["tenant_id", "project_id", "item_id"],
        "uq_cal_items_scope_id_round": ["tenant_id", "project_id", "item_id", "round_id"],
        "uq_cal_items_scope_round_pos": ["tenant_id", "project_id", "round_id", "ordinal"],
        "uq_cal_items_scope_round_case": [
            "tenant_id",
            "project_id",
            "round_id",
            "source_case_id",
        ],
    },
    "calibration_assignments": {
        "uq_cal_assign_scope_id": ["tenant_id", "project_id", "assignment_id"],
        "uq_cal_assign_scope_item_slot": [
            "tenant_id",
            "project_id",
            "round_id",
            "item_id",
            "slot",
        ],
        "uq_cal_assign_scope_item_reviewer": [
            "tenant_id",
            "project_id",
            "round_id",
            "item_id",
            "reviewer_id",
        ],
        "uq_cal_assign_scope_review_task": ["tenant_id", "project_id", "review_task_id"],
        "uq_cal_assign_scope_binding": [
            "tenant_id",
            "project_id",
            "assignment_id",
            "round_id",
            "item_id",
            "reviewer_id",
        ],
    },
    "calibration_submissions": {
        "uq_cal_subs_scope_id": ["tenant_id", "project_id", "submission_id"],
        "uq_cal_subs_scope_assignment": ["tenant_id", "project_id", "assignment_id"],
        "uq_cal_subs_scope_binding": [
            "tenant_id",
            "project_id",
            "submission_id",
            "round_id",
            "item_id",
        ],
    },
    "calibration_adjudications": {
        "uq_cal_adjud_scope_id": ["tenant_id", "project_id", "adjudication_id"],
        "uq_cal_adjud_scope_item": ["tenant_id", "project_id", "item_id"],
    },
    "gold_set_versions": {
        "uq_gold_versions_scope_id": ["tenant_id", "project_id", "gold_set_version_id"],
        "uq_gold_versions_scope_round": ["tenant_id", "project_id", "round_id"],
        "uq_gold_versions_scope_series": [
            "tenant_id",
            "project_id",
            "gold_set_key",
            "version_number",
        ],
        "uq_gold_versions_scope_binding": [
            "tenant_id",
            "project_id",
            "gold_set_version_id",
            "round_id",
        ],
    },
    "gold_set_series": {
        "uq_gold_series_scope_key": ["tenant_id", "project_id", "gold_set_key"],
    },
    "gold_annotations": {
        "uq_gold_annotations_scope_id": ["tenant_id", "project_id", "gold_annotation_id"],
        "uq_gold_annotations_scope_item": [
            "tenant_id",
            "project_id",
            "gold_set_version_id",
            "item_id",
        ],
    },
}

CALIBRATION_FOREIGN_KEYS = {
    "calibration_items": {
        "fk_cal_items_scope_round": (
            "calibration_rounds",
            ["tenant_id", "project_id", "round_id"],
            ["tenant_id", "project_id", "round_id"],
        ),
    },
    "calibration_assignments": {
        "fk_cal_assign_scope_item": (
            "calibration_items",
            ["tenant_id", "project_id", "item_id", "round_id"],
            ["tenant_id", "project_id", "item_id", "round_id"],
        ),
        "fk_cal_assign_scope_review_task": (
            "human_review_tasks",
            ["tenant_id", "project_id", "review_task_id"],
            ["tenant_id", "project_id", "review_task_id"],
        ),
    },
    "calibration_submissions": {
        "fk_cal_subs_scope_assignment_binding": (
            "calibration_assignments",
            [
                "tenant_id",
                "project_id",
                "assignment_id",
                "round_id",
                "item_id",
                "reviewer_id",
            ],
            [
                "tenant_id",
                "project_id",
                "assignment_id",
                "round_id",
                "item_id",
                "reviewer_id",
            ],
        ),
        "fk_cal_subs_scope_item": (
            "calibration_items",
            ["tenant_id", "project_id", "item_id", "round_id"],
            ["tenant_id", "project_id", "item_id", "round_id"],
        ),
    },
    "calibration_adjudications": {
        "fk_cal_adjud_scope_item": (
            "calibration_items",
            ["tenant_id", "project_id", "item_id", "round_id"],
            ["tenant_id", "project_id", "item_id", "round_id"],
        ),
        "fk_cal_adjud_scope_submission_binding": (
            "calibration_submissions",
            ["tenant_id", "project_id", "accepted_submission_id", "round_id", "item_id"],
            ["tenant_id", "project_id", "submission_id", "round_id", "item_id"],
        ),
    },
    "gold_set_versions": {
        "fk_gold_versions_scope_round": (
            "calibration_rounds",
            ["tenant_id", "project_id", "round_id"],
            ["tenant_id", "project_id", "round_id"],
        ),
    },
    "gold_annotations": {
        "fk_gold_annotations_scope_version_binding": (
            "gold_set_versions",
            ["tenant_id", "project_id", "gold_set_version_id", "round_id"],
            ["tenant_id", "project_id", "gold_set_version_id", "round_id"],
        ),
        "fk_gold_annotations_scope_item": (
            "calibration_items",
            ["tenant_id", "project_id", "item_id", "round_id"],
            ["tenant_id", "project_id", "item_id", "round_id"],
        ),
    },
}

CALIBRATION_CHECKS = {
    "calibration_rounds": {
        "ck_cal_rounds_participants",
        "ck_cal_rounds_status",
        "ck_cal_rounds_metrics",
        "ck_cal_rounds_resource_version",
        "ck_cal_rounds_completion_state",
        "ck_cal_rounds_publish_state",
        "ck_cal_rounds_kappa_defined",
    },
    "calibration_items": {
        "ck_cal_items_status",
        "ck_cal_items_review_outcome",
        "ck_cal_items_resolution_state",
        "ck_cal_items_ordinal",
        "ck_cal_items_resource_version",
        "ck_cal_items_claim_state",
    },
    "calibration_assignments": {
        "ck_cal_assign_slot",
        "ck_cal_assign_status",
        "ck_cal_assign_submit_state",
        "ck_cal_assign_resource_version",
    },
    "calibration_submissions": {
        "ck_cal_subs_resource_version",
        "ck_cal_subs_value_schema",
    },
    "calibration_adjudications": {
        "ck_cal_adjud_decision",
        "ck_cal_adjud_reason",
        "ck_cal_adjud_resolution",
        "ck_cal_adjud_resource_version",
    },
    "gold_set_versions": {
        "ck_gold_versions_status",
        "ck_gold_versions_metrics",
        "ck_gold_versions_resource_version",
        "ck_gold_versions_kappa_defined",
    },
    "gold_set_series": {"ck_gold_series_next_version"},
    "gold_annotations": {
        "ck_gold_annotations_source",
        "ck_gold_annotations_resource_version",
    },
}

CALIBRATION_INDEXES = {
    "calibration_rounds": {
        "ix_cal_rounds_scope_status",
        "ix_cal_rounds_scope_dataset",
    },
    "calibration_items": {"ix_cal_items_scope_round_status"},
    "calibration_assignments": {
        "ix_cal_assign_scope_reviewer",
        "ix_cal_assign_scope_round",
    },
    "calibration_submissions": {"ix_cal_subs_scope_round_item"},
    "calibration_adjudications": {"ix_cal_adjud_scope_round"},
    "gold_set_versions": {"ix_gold_versions_scope_series"},
    "gold_set_series": {"ix_gold_series_scope_key"},
    "gold_annotations": {"ix_gold_annotations_scope_version"},
}

CALIBRATION_RESOURCE_VERSION_TABLES = CALIBRATION_TABLES
CALIBRATION_METRIC_COLUMNS = {
    "calibration_rounds": {
        "paired_submission_count",
        "agreed_count",
        "conflict_count",
        "adjudication_count",
        "observed_agreement_ppm",
        "cohen_kappa_micros",
    },
    "gold_set_versions": {
        "conflict_count",
        "adjudication_count",
        "observed_agreement_ppm",
        "cohen_kappa_micros",
    },
}
CALIBRATION_BOOLEAN_COLUMNS = {
    "calibration_rounds": {"cohen_kappa_defined"},
    "gold_set_versions": {"cohen_kappa_defined", "legacy_empty_compatible"},
}

CALIBRATION_SUBMISSION_COMPATIBILITY_COLUMNS = {
    "legacy_value_json",
    "value_schema_version",
}


def alembic_config() -> Config:
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "migrations"))
    return config


def _normalized_check_sql(sqltext: object) -> str:
    return re.sub(r"[`\"\s]+", "", str(sqltext or "").lower())


def assert_hotword_schema(inspector: Any) -> None:
    for table_name, expected_columns in HOTWORD_COLUMNS.items():
        columns = {column["name"]: column for column in inspector.get_columns(table_name)}
        missing_columns = sorted(expected_columns - set(columns))
        if missing_columns:
            raise AssertionError(
                f"missing hotword columns on {table_name}: " + ", ".join(missing_columns)
            )
        for column_name in HOTWORD_NON_NULL_COLUMNS[table_name]:
            if columns[column_name].get("nullable") is not False:
                raise AssertionError(f"hotword column must be non-null: {table_name}.{column_name}")

        primary_key = inspector.get_pk_constraint(table_name)
        if primary_key.get("constrained_columns") != HOTWORD_PRIMARY_KEYS[table_name]:
            raise AssertionError(f"invalid hotword primary key on {table_name}: {primary_key!r}")

        resource_version = columns.get("resource_version")
        if resource_version is not None and not isinstance(resource_version.get("type"), Integer):
            raise AssertionError(f"hotword resource_version must be integer on {table_name}")

    version_columns = {
        column["name"]: column for column in inspector.get_columns("hotword_pack_versions")
    }
    eval_locked = version_columns["eval_locked"]
    eval_locked_type = str(eval_locked.get("type", "")).upper()
    if not isinstance(eval_locked.get("type"), Boolean) and not any(
        marker in eval_locked_type for marker in ("BOOL", "TINYINT")
    ):
        raise AssertionError("hotword_pack_versions.eval_locked must be boolean")

    item_columns = {
        column["name"]: column for column in inspector.get_columns("hotword_version_items")
    }
    if not isinstance(item_columns["weight"].get("type"), Integer):
        raise AssertionError("hotword_version_items.weight must be integer")

    badcase_columns = {column["name"]: column for column in inspector.get_columns("badcases")}
    missing_badcase_columns = sorted(HOTWORD_BADCASE_COLUMNS - set(badcase_columns))
    if missing_badcase_columns:
        raise AssertionError(
            "missing ASR hotword badcase columns: " + ", ".join(missing_badcase_columns)
        )
    for column_name in HOTWORD_BADCASE_NON_NULL_COLUMNS:
        if badcase_columns[column_name].get("nullable") is not False:
            raise AssertionError(f"ASR hotword badcase column must be non-null: {column_name}")
    for column_name in (
        "expected_count",
        "correct_count",
        "manual_correction_count",
        "resource_version",
    ):
        if not isinstance(badcase_columns[column_name].get("type"), Integer):
            raise AssertionError(f"ASR hotword badcase column must be integer: {column_name}")

    for table_name, expected_constraints in HOTWORD_UNIQUE_CONSTRAINTS.items():
        constraints = {
            constraint.get("name"): constraint
            for constraint in inspector.get_unique_constraints(table_name)
        }
        for constraint_name, expected_unique_columns in expected_constraints.items():
            constraint = constraints.get(constraint_name)
            if constraint is None:
                raise AssertionError(f"missing hotword scoped uniqueness: {constraint_name}")
            if constraint.get("column_names") != expected_unique_columns:
                raise AssertionError(
                    f"invalid columns for {constraint_name}: {constraint.get('column_names')!r}"
                )

    for table_name, expected_foreign_keys in HOTWORD_FOREIGN_KEYS.items():
        foreign_keys = {
            foreign_key.get("name"): foreign_key
            for foreign_key in inspector.get_foreign_keys(table_name)
        }
        for constraint_name, (
            expected_parent,
            expected_fk_columns,
            expected_parent_columns,
        ) in expected_foreign_keys.items():
            foreign_key = foreign_keys.get(constraint_name)
            if foreign_key is None:
                raise AssertionError(f"missing hotword scoped foreign key: {constraint_name}")
            if (
                foreign_key.get("referred_table") != expected_parent
                or foreign_key.get("constrained_columns") != expected_fk_columns
                or foreign_key.get("referred_columns") != expected_parent_columns
            ):
                raise AssertionError(
                    f"invalid hotword scoped foreign key {constraint_name}: {foreign_key!r}"
                )
            options = foreign_key.get("options") or {}
            if str(options.get("ondelete") or "NO ACTION").upper() not in {
                "NO ACTION",
                "RESTRICT",
            }:
                raise AssertionError(f"{constraint_name} must restrict deletes")
            if str(options.get("onupdate") or "NO ACTION").upper() not in {
                "NO ACTION",
                "RESTRICT",
            }:
                raise AssertionError(f"{constraint_name} must restrict updates")

    for table_name, expected_checks in HOTWORD_CHECK_FRAGMENTS.items():
        checks = {
            constraint.get("name"): constraint
            for constraint in inspector.get_check_constraints(table_name)
        }
        for constraint_name, required_fragments in expected_checks.items():
            constraint = checks.get(constraint_name)
            if constraint is None:
                raise AssertionError(f"missing hotword check: {constraint_name}")
            normalized_sql = _normalized_check_sql(constraint.get("sqltext"))
            for fragment in required_fragments:
                if _normalized_check_sql(fragment) not in normalized_sql:
                    raise AssertionError(
                        f"invalid hotword check {constraint_name}: {constraint.get('sqltext')!r}"
                    )
            expected_literals = HOTWORD_CHECK_LITERALS.get(constraint_name)
            if expected_literals is not None:
                actual_literals = set(
                    re.findall(r"'((?:''|[^'])*)'", str(constraint.get("sqltext") or ""))
                )
                if actual_literals != expected_literals:
                    raise AssertionError(
                        f"invalid allowed values for {constraint_name}: {sorted(actual_literals)!r}"
                    )

    for table_name, expected_indexes in HOTWORD_INDEXES.items():
        indexes = {index.get("name"): index for index in inspector.get_indexes(table_name)}
        for index_name, expected_index_columns in expected_indexes.items():
            index = indexes.get(index_name)
            if index is None:
                raise AssertionError(f"missing hotword index: {index_name}")
            if index.get("column_names") != expected_index_columns:
                raise AssertionError(
                    f"invalid columns for {index_name}: {index.get('column_names')!r}"
                )


def _seed_record_index(
    records: object,
    id_key: str,
    label: str,
) -> dict[str, dict[str, Any]]:
    if not isinstance(records, list):
        raise AssertionError(f"hotword seed {label} must be a list")
    indexed: dict[str, dict[str, Any]] = {}
    for position, record in enumerate(records):
        if not isinstance(record, dict):
            raise AssertionError(f"hotword seed {label}[{position}] must be an object")
        record_id = record.get(id_key)
        if not isinstance(record_id, str) or not record_id:
            raise AssertionError(f"hotword seed {label}[{position}] is missing {id_key}")
        if record_id in indexed:
            raise AssertionError(f"duplicate hotword seed {id_key}: {record_id}")
        indexed[record_id] = record
    return indexed


def assert_hotword_seed_references(seed_path: Path) -> None:
    seed = json.loads(seed_path.read_text(encoding="utf-8"))
    if not isinstance(seed, dict):
        raise AssertionError("hotword seed root must be an object")

    governance = seed.get("hotword_governance")
    if not isinstance(governance, dict):
        raise AssertionError("hotword seed is missing hotword_governance")
    evaluation = seed.get("evaluation")
    if not isinstance(evaluation, dict):
        raise AssertionError("hotword seed is missing evaluation")
    review = seed.get("review_and_feedback")
    if not isinstance(review, dict):
        raise AssertionError("hotword seed is missing review_and_feedback")
    context = seed.get("context")
    if not isinstance(context, dict):
        raise AssertionError("hotword seed is missing context")

    packs = _seed_record_index(governance.get("hotword_packs"), "pack_id", "hotword_packs")
    versions = _seed_record_index(
        governance.get("hotword_pack_versions"),
        "version_id",
        "hotword_pack_versions",
    )
    items = _seed_record_index(
        governance.get("hotword_version_items"),
        "item_id",
        "hotword_version_items",
    )
    snapshots = _seed_record_index(
        governance.get("hotword_metric_snapshots"),
        "snapshot_id",
        "hotword_metric_snapshots",
    )
    analysis_runs = _seed_record_index(
        governance.get("analysis_runs"),
        "run_id",
        "analysis_runs",
    )
    storage_objects = _seed_record_index(
        governance.get("storage_objects"),
        "storage_object_id",
        "storage_objects",
    )
    badcases = _seed_record_index(review.get("badcases"), "badcase_id", "badcases")
    eval_runs = _seed_record_index(evaluation.get("eval_runs"), "eval_run_id", "eval_runs")
    eval_datasets = _seed_record_index(
        evaluation.get("eval_datasets"),
        "dataset_id",
        "eval_datasets",
    )
    users = _seed_record_index(context.get("users"), "user_id", "users")

    for badcase_id, badcase in badcases.items():
        if badcase.get("capability") != "asr-hotword":
            continue
        evidence_storage_object_id = badcase.get("evidence_storage_object_id")
        if not isinstance(evidence_storage_object_id, str) or not evidence_storage_object_id:
            raise AssertionError(
                f"ASR hotword badcase {badcase_id} is missing evidence_storage_object_id"
            )
        evidence_object = storage_objects.get(evidence_storage_object_id)
        if evidence_object is None:
            raise AssertionError(
                f"ASR hotword badcase {badcase_id} evidence_storage_object_id references "
                f"missing storage object {evidence_storage_object_id}"
            )
        if evidence_object.get("source_id") != badcase_id:
            raise AssertionError(
                f"ASR hotword badcase {badcase_id} evidence object source_id is inconsistent"
            )
        expected_evidence_ref = f"storage-object:{evidence_storage_object_id}"
        if badcase.get("evidence_ref") != expected_evidence_ref:
            raise AssertionError(
                f"ASR hotword badcase {badcase_id} evidence_ref must be {expected_evidence_ref}"
            )

    for pack_id, pack in packs.items():
        current_version_id = pack.get("current_version_id")
        if current_version_id is None:
            continue
        current_version = versions.get(str(current_version_id))
        if current_version is None:
            raise AssertionError(
                f"hotword pack {pack_id} current_version_id references missing-version "
                f"{current_version_id}"
            )
        if current_version.get("pack_id") != pack_id:
            raise AssertionError(
                f"hotword pack {pack_id} current_version_id belongs to another pack: "
                f"{current_version_id}"
            )
        production_version_id = pack.get("production_version_id")
        if production_version_id is None:
            continue
        production_version = versions.get(str(production_version_id))
        if production_version is None:
            raise AssertionError(
                f"hotword pack {pack_id} production_version_id references missing version "
                f"{production_version_id}"
            )
        if production_version.get("pack_id") != pack_id:
            raise AssertionError(
                f"hotword pack {pack_id} production_version_id belongs to another pack: "
                f"{production_version_id}"
            )
        if production_version.get("status") != "published":
            raise AssertionError(
                f"hotword pack {pack_id} production_version_id must reference a published version"
            )
        if production_version.get("production_active") is not True:
            raise AssertionError(
                f"hotword pack {pack_id} production_version_id must be production_active"
            )

    for version_id, version in versions.items():
        version_pack_id = version.get("pack_id")
        if version_pack_id not in packs:
            raise AssertionError(
                f"hotword version {version_id} pack_id references missing pack {version_pack_id}"
            )
        baseline_version_id = version.get("baseline_version_id")
        if baseline_version_id is not None:
            baseline = versions.get(str(baseline_version_id))
            if baseline is None:
                raise AssertionError(
                    f"hotword version {version_id} baseline_version_id references "
                    f"missing version {baseline_version_id}"
                )
            if baseline.get("pack_id") != version_pack_id:
                raise AssertionError(
                    f"hotword version {version_id} baseline_version_id belongs to another pack"
                )

        eval_run_id = version.get("eval_run_id")
        if eval_run_id is not None:
            eval_run = eval_runs.get(str(eval_run_id))
            if eval_run is None:
                raise AssertionError(
                    f"hotword version {version_id} eval_run_id references missing run {eval_run_id}"
                )
            if eval_run.get("candidate_version") != version_id:
                raise AssertionError(
                    f"hotword version {version_id} eval_run_id candidate binding is inconsistent"
                )
            dataset_id = eval_run.get("dataset_id")
            if dataset_id not in eval_datasets:
                raise AssertionError(
                    f"hotword eval run {eval_run_id} dataset_id references missing dataset "
                    f"{dataset_id}"
                )

        for approval_key in ("model_approved_by", "project_admin_confirmed_by"):
            approver_id = version.get(approval_key)
            if approver_id is not None and approver_id not in users:
                raise AssertionError(
                    f"hotword version {version_id} {approval_key} references missing user "
                    f"{approver_id}"
                )

    for item_id, item in items.items():
        item_version_id = item.get("version_id")
        if item_version_id not in versions:
            raise AssertionError(
                f"hotword item {item_id} version_id references missing version {item_version_id}"
            )
        source_badcase_id = item.get("source_badcase_id")
        if source_badcase_id is not None:
            source_badcase = badcases.get(str(source_badcase_id))
            if source_badcase is None:
                raise AssertionError(
                    f"hotword item {item_id} source_badcase_id references missing badcase "
                    f"{source_badcase_id}"
                )
            if source_badcase.get("capability") != "asr-hotword":
                raise AssertionError(
                    f"hotword item {item_id} source_badcase_id is not an ASR hotword badcase"
                )
        if item.get("source_type") == "badcase" and source_badcase_id is None:
            raise AssertionError(
                f"hotword item {item_id} has badcase source_type without a badcase"
            )

    for snapshot_id, snapshot in snapshots.items():
        snapshot_version_id = snapshot.get("hotword_pack_version_id")
        if snapshot_version_id is not None and snapshot_version_id not in versions:
            raise AssertionError(
                f"hotword snapshot {snapshot_id} hotword_pack_version_id references "
                f"missing version {snapshot_version_id}"
            )
        source_run_id = snapshot.get("source_run_id")
        if source_run_id not in analysis_runs:
            raise AssertionError(
                f"hotword snapshot {snapshot_id} source_run_id references missing run "
                f"{source_run_id}"
            )

    def assert_version_references(value: object, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}.{key}" if path else key
                if key == "hotword_pack_version_id" and child is not None and child not in versions:
                    raise AssertionError(
                        f"seed hotword_pack_version_id at {child_path} references "
                        f"missing-version {child}"
                    )
                assert_version_references(child, child_path)
        elif isinstance(value, list):
            for position, child in enumerate(value):
                assert_version_references(child, f"{path}[{position}]")

    assert_version_references(seed, "")


def assert_calibration_schema(inspector: Any) -> None:
    submission_columns = {
        column["name"]: column for column in inspector.get_columns("calibration_submissions")
    }
    missing_submission_columns = sorted(
        CALIBRATION_SUBMISSION_COMPATIBILITY_COLUMNS - set(submission_columns)
    )
    if missing_submission_columns:
        raise AssertionError(
            "missing calibration submission compatibility columns: "
            + ", ".join(missing_submission_columns)
        )
    schema_version = submission_columns["value_schema_version"]
    if schema_version.get("nullable") is not False or not isinstance(
        schema_version.get("type"), Integer
    ):
        raise AssertionError(
            "calibration_submissions.value_schema_version must be a non-null integer"
        )

    for table_name in CALIBRATION_RESOURCE_VERSION_TABLES:
        columns = {column["name"]: column for column in inspector.get_columns(table_name)}
        resource_version = columns.get("resource_version")
        if resource_version is None:
            raise AssertionError(f"missing calibration resource_version on {table_name}")
        if resource_version.get("nullable") is not False:
            raise AssertionError(f"calibration resource_version must be non-null on {table_name}")
        if not isinstance(resource_version.get("type"), Integer):
            raise AssertionError(f"calibration resource_version must be integer on {table_name}")

    for table_name, metric_names in CALIBRATION_METRIC_COLUMNS.items():
        columns = {column["name"]: column for column in inspector.get_columns(table_name)}
        missing_metrics = sorted(metric_names - set(columns))
        if missing_metrics:
            raise AssertionError(
                f"missing integer calibration metrics on {table_name}: "
                + ", ".join(missing_metrics)
            )
        for metric_name in metric_names:
            if not isinstance(columns[metric_name].get("type"), Integer):
                raise AssertionError(
                    f"calibration metric must be integer: {table_name}.{metric_name}"
                )
        if "conflict_rate_ppm" in columns:
            raise AssertionError(f"{table_name} must persist conflict counts, not a conflict rate")

    for table_name, flag_names in CALIBRATION_BOOLEAN_COLUMNS.items():
        columns = {column["name"]: column for column in inspector.get_columns(table_name)}
        for flag_name in flag_names:
            column = columns.get(flag_name)
            if column is None:
                raise AssertionError(f"missing calibration flag: {table_name}.{flag_name}")
            type_name = str(column.get("type", "")).upper()
            if not isinstance(column.get("type"), Boolean) and not any(
                marker in type_name for marker in ("BOOL", "TINYINT")
            ):
                raise AssertionError(f"calibration flag must be boolean: {table_name}.{flag_name}")
            if column.get("nullable") is not False:
                raise AssertionError(f"calibration flag must be non-null: {table_name}.{flag_name}")

    for table_name, expected_constraints in CALIBRATION_UNIQUE_CONSTRAINTS.items():
        constraints = {
            constraint.get("name"): constraint
            for constraint in inspector.get_unique_constraints(table_name)
        }
        for constraint_name, expected_columns in expected_constraints.items():
            constraint = constraints.get(constraint_name)
            if constraint is None:
                raise AssertionError(f"missing calibration uniqueness: {constraint_name}")
            if constraint.get("column_names") != expected_columns:
                raise AssertionError(
                    f"invalid columns for {constraint_name}: {constraint.get('column_names')!r}"
                )

    for table_name, expected_foreign_keys in CALIBRATION_FOREIGN_KEYS.items():
        foreign_keys = {
            foreign_key.get("name"): foreign_key
            for foreign_key in inspector.get_foreign_keys(table_name)
        }
        for constraint_name, (
            expected_parent,
            expected_columns,
            expected_parent_columns,
        ) in expected_foreign_keys.items():
            foreign_key = foreign_keys.get(constraint_name)
            if foreign_key is None:
                raise AssertionError(f"missing calibration foreign key: {constraint_name}")
            if (
                foreign_key.get("referred_table") != expected_parent
                or foreign_key.get("constrained_columns") != expected_columns
                or foreign_key.get("referred_columns") != expected_parent_columns
            ):
                raise AssertionError(
                    f"invalid calibration foreign key {constraint_name}: {foreign_key!r}"
                )
            options = foreign_key.get("options") or {}
            if str(options.get("ondelete") or "NO ACTION").upper() not in {
                "NO ACTION",
                "RESTRICT",
            }:
                raise AssertionError(f"{constraint_name} must restrict deletes")
            if str(options.get("onupdate") or "NO ACTION").upper() not in {
                "NO ACTION",
                "RESTRICT",
            }:
                raise AssertionError(f"{constraint_name} must restrict updates")

    for table_name, expected_checks in CALIBRATION_CHECKS.items():
        actual_checks = {
            constraint.get("name") for constraint in inspector.get_check_constraints(table_name)
        }
        missing_checks = sorted(expected_checks - actual_checks)
        if missing_checks:
            raise AssertionError(
                f"missing calibration checks on {table_name}: " + ", ".join(missing_checks)
            )

    for table_name, expected_indexes in CALIBRATION_INDEXES.items():
        actual_indexes = {index.get("name") for index in inspector.get_indexes(table_name)}
        missing_indexes = sorted(expected_indexes - actual_indexes)
        if missing_indexes:
            raise AssertionError(
                f"missing calibration indexes on {table_name}: " + ", ".join(missing_indexes)
            )


def assert_label_eval_schema(inspector: Any) -> None:
    for table_name in LABEL_EVAL_TABLES:
        actual_columns = {column["name"] for column in inspector.get_columns(table_name)}
        missing_columns = sorted(LABEL_EVAL_COLUMNS[table_name] - actual_columns)
        if missing_columns:
            raise AssertionError(
                f"missing label eval columns on {table_name}: " + ", ".join(missing_columns)
            )
        actual_indexes = {index.get("name") for index in inspector.get_indexes(table_name)}
        missing_indexes = sorted(LABEL_EVAL_INDEXES[table_name] - actual_indexes)
        if missing_indexes:
            raise AssertionError(
                f"missing label eval indexes on {table_name}: " + ", ".join(missing_indexes)
            )
        actual_uniques = {
            constraint.get("name") for constraint in inspector.get_unique_constraints(table_name)
        }
        missing_uniques = sorted(LABEL_EVAL_UNIQUES[table_name] - actual_uniques)
        if missing_uniques:
            raise AssertionError(
                f"missing label eval unique constraints on {table_name}: "
                + ", ".join(missing_uniques)
            )
        actual_checks = {
            constraint.get("name") for constraint in inspector.get_check_constraints(table_name)
        }
        missing_checks = sorted(LABEL_EVAL_CHECKS[table_name] - actual_checks)
        if missing_checks:
            raise AssertionError(
                f"missing label eval checks on {table_name}: " + ", ".join(missing_checks)
            )


def assert_label_lifecycle_schema(inspector: Any) -> None:
    tables = set(inspector.get_table_names())
    missing_tables = sorted(LABEL_LIFECYCLE_TABLES - tables)
    if missing_tables:
        raise AssertionError("missing label lifecycle tables: " + ", ".join(missing_tables))

    for table_name, expected_columns in LABEL_LIFECYCLE_COLUMNS.items():
        columns = {column["name"]: column for column in inspector.get_columns(table_name)}
        missing_columns = sorted(expected_columns - set(columns))
        if missing_columns:
            raise AssertionError(
                f"missing label lifecycle columns on {table_name}: " + ", ".join(missing_columns)
            )
    for column_name in LABEL_LIFECYCLE_COLUMNS["label_versions"]:
        column = {value["name"]: value for value in inspector.get_columns("label_versions")}[
            column_name
        ]
        if column.get("nullable") is not True:
            raise AssertionError(f"label_versions.{column_name} must stay nullable during expand")
    definition_column = {
        value["name"]: value for value in inspector.get_columns("label_version_items")
    }["definition_sha256"]
    if definition_column.get("nullable") is not True:
        raise AssertionError(
            "label_version_items.definition_sha256 must stay nullable during expand"
        )
    metric_result_columns = {
        value["name"]: value for value in inspector.get_columns("metric_results")
    }
    for column_name in LABEL_LIFECYCLE_COLUMNS["metric_results"]:
        if metric_result_columns[column_name].get("nullable") is not True:
            raise AssertionError(f"metric_results.{column_name} must stay nullable during expand")

    for table_name, expected_names in LABEL_LIFECYCLE_UNIQUES.items():
        actual_names = {
            constraint.get("name") for constraint in inspector.get_unique_constraints(table_name)
        }
        missing_names = sorted(expected_names - actual_names)
        if missing_names:
            raise AssertionError(
                f"missing label lifecycle uniqueness on {table_name}: " + ", ".join(missing_names)
            )

    for table_name, expected_names in LABEL_LIFECYCLE_CHECKS.items():
        actual_names = {
            constraint.get("name") for constraint in inspector.get_check_constraints(table_name)
        }
        missing_names = sorted(expected_names - actual_names)
        if missing_names:
            raise AssertionError(
                f"missing label lifecycle checks on {table_name}: " + ", ".join(missing_names)
            )

    for table_name, expected_names in LABEL_LIFECYCLE_INDEXES.items():
        actual_names = {index.get("name") for index in inspector.get_indexes(table_name)}
        missing_names = sorted(expected_names - actual_names)
        if missing_names:
            raise AssertionError(
                f"missing label lifecycle indexes on {table_name}: " + ", ".join(missing_names)
            )

    for table_name, expected_keys in LABEL_LIFECYCLE_FOREIGN_KEYS.items():
        actual_keys = {
            foreign_key.get("name"): foreign_key
            for foreign_key in inspector.get_foreign_keys(table_name)
        }
        for constraint_name, referred_table in expected_keys.items():
            foreign_key = actual_keys.get(constraint_name)
            if foreign_key is None:
                raise AssertionError(f"missing label lifecycle foreign key: {constraint_name}")
            if foreign_key.get("referred_table") != referred_table:
                raise AssertionError(f"invalid parent for {constraint_name}: {foreign_key!r}")
            if foreign_key.get("constrained_columns", [])[:2] != [
                "tenant_id",
                "project_id",
            ]:
                raise AssertionError(
                    f"label lifecycle foreign key is not project scoped: {constraint_name}"
                )

    if inspector.bind.dialect.name == "sqlite":
        append_only_tables = {
            "metric_results",
            "metric_result_label_scopes",
            "insight_report_metric_bindings",
        }
        expected_triggers = {
            f"trg_{table_name}_no_{action}"
            for table_name in append_only_tables
            for action in ("update", "delete")
        }
        with inspector.bind.connect() as connection:
            actual_triggers = {
                str(row[0])
                for row in connection.execute(
                    text(
                        "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                        "AND tbl_name IN ('metric_results', "
                        "'metric_result_label_scopes', "
                        "'insight_report_metric_bindings')"
                    )
                )
            }
        missing_triggers = sorted(expected_triggers - actual_triggers)
        if missing_triggers:
            raise AssertionError(
                "missing label metric append-only triggers: " + ", ".join(missing_triggers)
            )


def assert_tables_present(database_url: str) -> None:
    engine = create_engine(database_url, future=True)
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        missing = sorted(
            (
                CORE_TABLES
                | DOMAIN_BASELINE_TABLES
                | CALIBRATION_TABLES
                | HOTWORD_TABLES
                | LABEL_EVAL_TABLES
                | LABEL_LIFECYCLE_TABLES
            )
            - tables
        )
        if missing:
            raise AssertionError(f"missing migrated tables: {', '.join(missing)}")

        browser_session_columns = {
            column["name"] for column in inspector.get_columns("browser_auth_sessions")
        }
        missing_browser_columns = sorted(OIDC_BROWSER_SESSION_COLUMNS - browser_session_columns)
        if missing_browser_columns:
            raise AssertionError(
                "missing OIDC browser-session columns: " + ", ".join(missing_browser_columns)
            )
        forbidden_raw_columns = {"token", "raw_token", "csrf_token"} & browser_session_columns
        if forbidden_raw_columns:
            raise AssertionError(
                "browser sessions must store only token hashes: "
                + ", ".join(sorted(forbidden_raw_columns))
            )
        identity_columns = {column["name"] for column in inspector.get_columns("oidc_identities")}
        if "subject" in identity_columns or "subject_sha256" not in identity_columns:
            raise AssertionError("OIDC identities must persist only the subject hash")
        with engine.connect() as connection:
            user_count = connection.scalar(text("SELECT COUNT(*) FROM users"))
            security_count = connection.scalar(text("SELECT COUNT(*) FROM user_security_states"))
        if user_count != security_count:
            raise AssertionError("every migrated user must have an explicit security state")

        run_record_columns = {
            column["name"]: column for column in inspector.get_columns("run_records")
        }
        missing_task_run_control_columns = sorted(
            TASK_RUN_CONTROL_COLUMNS - set(run_record_columns)
        )
        if missing_task_run_control_columns:
            raise AssertionError(
                "missing task-run control columns: " + ", ".join(missing_task_run_control_columns)
            )
        if run_record_columns["status_version"].get("nullable") is not False:
            raise AssertionError("task-run status version must be non-null")
        if run_record_columns["monitor_generation"].get("nullable") is not False:
            raise AssertionError("task-run monitor generation must be non-null")
        run_record_indexes = {
            index.get("name"): index for index in inspector.get_indexes("run_records")
        }
        missing_task_run_control_indexes = sorted(
            TASK_RUN_CONTROL_INDEXES - set(run_record_indexes)
        )
        if missing_task_run_control_indexes:
            raise AssertionError(
                "missing task-run control indexes: " + ", ".join(missing_task_run_control_indexes)
            )
        for index_name, expected_columns in TASK_RUN_CONTROL_INDEX_COLUMNS.items():
            actual_columns = run_record_indexes[index_name].get("column_names")
            if actual_columns != expected_columns:
                raise AssertionError(
                    f"task-run control index {index_name} must use columns "
                    f"{expected_columns}, got {actual_columns}"
                )

        assert_label_lifecycle_schema(inspector)

        unique_constraints = inspector.get_unique_constraints("json_resources")
        if not any(
            constraint.get("name") == "uq_json_resources_scope_key"
            for constraint in unique_constraints
        ):
            raise AssertionError("missing json_resources scoped unique constraint")

        storage_constraints = inspector.get_unique_constraints("storage_objects")
        if not any(
            constraint.get("name") == "uq_storage_objects_scope_locator"
            for constraint in storage_constraints
        ):
            raise AssertionError("missing storage_objects scoped locator constraint")

        outbox_columns = {
            column["name"]: column for column in inspector.get_columns("outbox_events")
        }
        missing_outbox_columns = sorted(OUTBOX_LEASE_COLUMNS - set(outbox_columns))
        if missing_outbox_columns:
            raise AssertionError(
                f"missing outbox lease columns: {', '.join(missing_outbox_columns)}"
            )
        if outbox_columns["dispatch_idempotency_key"].get("nullable") is not False:
            raise AssertionError("outbox dispatch idempotency key must be non-null")
        if outbox_columns["lease_generation"].get("nullable") is not False:
            raise AssertionError("outbox lease generation must be non-null")

        outbox_indexes = {
            index.get("name"): index for index in inspector.get_indexes("outbox_events")
        }
        missing_outbox_indexes = sorted(OUTBOX_LEASE_INDEXES - set(outbox_indexes))
        if missing_outbox_indexes:
            raise AssertionError(
                f"missing outbox lease indexes: {', '.join(missing_outbox_indexes)}"
            )
        if not outbox_indexes["uq_outbox_events_dispatch_idempotency_key"].get("unique"):
            raise AssertionError("outbox dispatch idempotency index must be unique")

        attempt_indexes = {
            index.get("name"): index for index in inspector.get_indexes("outbox_delivery_attempts")
        }
        missing_attempt_indexes = sorted(OUTBOX_ATTEMPT_INDEXES - set(attempt_indexes))
        if missing_attempt_indexes:
            raise AssertionError(
                f"missing outbox delivery attempt indexes: {', '.join(missing_attempt_indexes)}"
            )
        attempt_constraints = inspector.get_unique_constraints("outbox_delivery_attempts")
        if not any(
            constraint.get("name") == "uq_outbox_delivery_attempts_event_generation"
            for constraint in attempt_constraints
        ):
            raise AssertionError("missing outbox attempt generation uniqueness constraint")
        attempt_foreign_keys = inspector.get_foreign_keys("outbox_delivery_attempts")
        if not any(
            foreign_key.get("referred_table") == "outbox_events"
            and foreign_key.get("constrained_columns") == ["event_id"]
            for foreign_key in attempt_foreign_keys
        ):
            raise AssertionError("missing outbox attempt to event foreign key")

        label_version_columns = {
            column["name"] for column in inspector.get_columns("label_versions")
        }
        missing_label_columns = sorted(LABEL_VERSION_POLICY_COLUMNS - label_version_columns)
        if missing_label_columns:
            raise AssertionError(
                f"missing label policy binding columns: {', '.join(missing_label_columns)}"
            )
        candidate_columns = {column["name"] for column in inspector.get_columns("label_candidates")}
        if "resource_version" not in candidate_columns:
            raise AssertionError("missing label candidate resource version")

        policy_constraints = inspector.get_unique_constraints("label_policy_versions")
        if not any(
            constraint.get("name") == "uq_label_policy_versions_scope_artifact"
            for constraint in policy_constraints
        ):
            raise AssertionError("missing label policy artifact uniqueness constraint")
        evaluation_constraints = inspector.get_unique_constraints("label_policy_evaluations")
        if not any(
            constraint.get("name") == "uq_label_policy_evaluations_replay"
            for constraint in evaluation_constraints
        ):
            raise AssertionError("missing label policy replay uniqueness constraint")

        for table_name, constraint_name in INSIGHT_UNIQUE_CONSTRAINTS.items():
            constraints = inspector.get_unique_constraints(table_name)
            if not any(constraint.get("name") == constraint_name for constraint in constraints):
                raise AssertionError(
                    f"missing governed insight uniqueness constraint: {constraint_name}"
                )

        for table_name, (
            constraint_name,
            expected_columns,
        ) in INSIGHT_SCOPE_UNIQUE_CONSTRAINTS.items():
            scoped_constraints = {
                constraint.get("name"): constraint
                for constraint in inspector.get_unique_constraints(table_name)
            }
            constraint = scoped_constraints.get(constraint_name)
            if constraint is None:
                raise AssertionError(
                    f"missing insight scoped-ID uniqueness constraint: {constraint_name}"
                )
            if constraint.get("column_names") != expected_columns:
                raise AssertionError(
                    f"invalid columns for {constraint_name}: {constraint.get('column_names')!r}"
                )

        for table_name, expected_foreign_keys in INSIGHT_CAUSAL_FOREIGN_KEYS.items():
            foreign_keys = {
                foreign_key.get("name"): foreign_key
                for foreign_key in inspector.get_foreign_keys(table_name)
            }
            for constraint_name, (
                expected_parent,
                expected_columns,
                expected_parent_columns,
            ) in expected_foreign_keys.items():
                foreign_key = foreign_keys.get(constraint_name)
                if foreign_key is None:
                    raise AssertionError(f"missing insight causal foreign key: {constraint_name}")
                if (
                    foreign_key.get("referred_table") != expected_parent
                    or foreign_key.get("constrained_columns") != expected_columns
                    or foreign_key.get("referred_columns") != expected_parent_columns
                ):
                    raise AssertionError(
                        f"invalid definition for {constraint_name}: {foreign_key!r}"
                    )
                options = foreign_key.get("options") or {}
                ondelete = str(options.get("ondelete") or "NO ACTION").upper()
                onupdate = str(options.get("onupdate") or "NO ACTION").upper()
                if ondelete not in {"NO ACTION", "RESTRICT"}:
                    raise AssertionError(f"{constraint_name} must restrict deletes, got {ondelete}")
                if onupdate not in {"NO ACTION", "RESTRICT"}:
                    raise AssertionError(f"{constraint_name} must restrict updates, got {onupdate}")

        for table_name, expected_indexes in INSIGHT_CAUSAL_INDEXES.items():
            actual_indexes = {index.get("name") for index in inspector.get_indexes(table_name)}
            missing_indexes = sorted(expected_indexes - actual_indexes)
            if missing_indexes:
                raise AssertionError(
                    f"missing insight causal indexes on {table_name}: " + ", ".join(missing_indexes)
                )

        human_review_columns = {
            column["name"]: column for column in inspector.get_columns("human_review_decisions")
        }
        missing_review_columns = sorted(HUMAN_REVIEW_DECISION_COLUMNS - set(human_review_columns))
        if missing_review_columns:
            raise AssertionError(
                "missing human review decision columns: " + ", ".join(missing_review_columns)
            )
        if human_review_columns["review_task_id"].get("nullable") is not False:
            raise AssertionError("human review decision review_task_id must be non-null")
        review_task_constraints = inspector.get_unique_constraints("human_review_tasks")
        if not any(
            constraint.get("name") == "uq_human_review_tasks_scope"
            for constraint in review_task_constraints
        ):
            raise AssertionError("missing scoped human review task uniqueness constraint")
        review_constraints = inspector.get_unique_constraints("human_review_decisions")
        if not any(
            constraint.get("name") == "uq_human_review_decisions_scope"
            for constraint in review_constraints
        ):
            raise AssertionError("missing scoped human review decision uniqueness constraint")
        if not any(
            constraint.get("name") == "uq_human_review_decisions_terminal_task"
            for constraint in review_constraints
        ):
            raise AssertionError("missing human review terminal-decision uniqueness constraint")
        if not any(
            constraint.get("name") == "uq_human_review_decisions_scope_terminal_binding"
            for constraint in review_constraints
        ):
            raise AssertionError("missing terminal decision binding constraint")
        review_indexes = {
            index.get("name") for index in inspector.get_indexes("human_review_decisions")
        }
        if "ix_human_review_decisions_scope_task" not in review_indexes:
            raise AssertionError("missing human review task lookup index")

        appeal_columns = {
            column["name"]: column for column in inspector.get_columns("quality_appeals")
        }
        missing_appeal_columns = sorted(QUALITY_APPEAL_COLUMNS - set(appeal_columns))
        if missing_appeal_columns:
            raise AssertionError(
                "missing quality appeal columns: " + ", ".join(missing_appeal_columns)
            )
        for frozen_column in (
            "source_decision_id",
            "source_review_task_id",
            "review_task_id",
            "source_result_sha256",
            "source_decider_id",
            "source_trace_id",
            "root_trace_id",
            "current_trace_id",
            "appellant_id",
            "evidence_refs",
        ):
            if appeal_columns[frozen_column].get("nullable") is not False:
                raise AssertionError(f"quality appeal {frozen_column} must be non-null")

        appeal_constraints = {
            constraint.get("name"): constraint
            for constraint in inspector.get_unique_constraints("quality_appeals")
        }
        for constraint_name in (
            "uq_quality_appeals_scope_id",
            "uq_quality_appeals_scope_source_decision",
            "uq_quality_appeals_scope_review_task",
            "uq_quality_appeals_scope_appeal_decision",
        ):
            if constraint_name not in appeal_constraints:
                raise AssertionError(f"missing quality appeal uniqueness: {constraint_name}")

        appeal_foreign_keys = {
            foreign_key.get("name"): foreign_key
            for foreign_key in inspector.get_foreign_keys("quality_appeals")
        }
        expected_appeal_foreign_keys = {
            "fk_quality_appeals_scope_terminal_decision": (
                "human_review_decisions",
                [
                    "tenant_id",
                    "project_id",
                    "source_decision_id",
                    "source_review_task_id",
                ],
                [
                    "tenant_id",
                    "project_id",
                    "decision_id",
                    "terminal_review_task_id",
                ],
            ),
            "fk_quality_appeals_scope_review_task": (
                "human_review_tasks",
                ["tenant_id", "project_id", "review_task_id"],
                ["tenant_id", "project_id", "review_task_id"],
            ),
            "fk_quality_appeals_scope_appeal_decision": (
                "human_review_decisions",
                ["tenant_id", "project_id", "appeal_decision_id", "review_task_id"],
                [
                    "tenant_id",
                    "project_id",
                    "decision_id",
                    "terminal_review_task_id",
                ],
            ),
        }
        for constraint_name, (
            parent_table,
            constrained_columns,
            referred_columns,
        ) in expected_appeal_foreign_keys.items():
            foreign_key = appeal_foreign_keys.get(constraint_name)
            if foreign_key is None:
                raise AssertionError(f"missing quality appeal foreign key: {constraint_name}")
            if (
                foreign_key.get("referred_table") != parent_table
                or foreign_key.get("constrained_columns") != constrained_columns
                or foreign_key.get("referred_columns") != referred_columns
            ):
                raise AssertionError(
                    f"invalid quality appeal binding {constraint_name}: {foreign_key!r}"
                )
            options = foreign_key.get("options") or {}
            if str(options.get("ondelete") or "NO ACTION").upper() not in {
                "NO ACTION",
                "RESTRICT",
            }:
                raise AssertionError(f"{constraint_name} must restrict deletes")

        appeal_indexes = {index.get("name") for index in inspector.get_indexes("quality_appeals")}
        missing_appeal_indexes = sorted(QUALITY_APPEAL_INDEXES - appeal_indexes)
        if missing_appeal_indexes:
            raise AssertionError(
                "missing quality appeal indexes: " + ", ".join(missing_appeal_indexes)
            )
        appeal_checks = {
            constraint.get("name")
            for constraint in inspector.get_check_constraints("quality_appeals")
        }
        missing_appeal_checks = sorted(QUALITY_APPEAL_CHECKS - appeal_checks)
        if missing_appeal_checks:
            raise AssertionError(
                "missing quality appeal checks: " + ", ".join(missing_appeal_checks)
            )
        assert_calibration_schema(inspector)
        assert_hotword_schema(inspector)
        assert_label_eval_schema(inspector)
    finally:
        engine.dispose()


def insert_legacy_outbox_event(database_url: str) -> int:
    engine = create_engine(database_url, future=True)
    try:
        with engine.begin() as connection:
            result = connection.execute(
                text(
                    "INSERT INTO outbox_events "
                    "(tenant_id, project_id, event_type, aggregate_type, aggregate_id, "
                    "status, payload, attempt_count) "
                    "VALUES (:tenant_id, :project_id, :event_type, :aggregate_type, "
                    ":aggregate_id, :status, :payload, :attempt_count)"
                ),
                {
                    "tenant_id": "migration_tenant",
                    "project_id": "migration_project",
                    "event_type": "migration.legacy.requested",
                    "aggregate_type": "migration_probe",
                    "aggregate_id": "legacy-outbox-event",
                    "status": "pending",
                    "payload": json.dumps({"probe": True}),
                    "attempt_count": 0,
                },
            )
            event_id = result.lastrowid
        if not isinstance(event_id, int):
            raise AssertionError("legacy outbox insert did not return an integer event id")
        return event_id
    finally:
        engine.dispose()


def assert_legacy_outbox_backfilled(database_url: str, event_id: int) -> None:
    engine = create_engine(database_url, future=True)
    try:
        with engine.connect() as connection:
            dispatch_key = connection.scalar(
                text(
                    "SELECT dispatch_idempotency_key FROM outbox_events WHERE event_id = :event_id"
                ),
                {"event_id": event_id},
            )
        expected = f"outbox_legacy_{event_id:020d}"
        if dispatch_key != expected:
            raise AssertionError(
                f"legacy outbox idempotency backfill mismatch: {dispatch_key!r} != {expected!r}"
            )
    finally:
        engine.dispose()


def insert_legacy_human_review_decisions(database_url: str) -> tuple[str, str]:
    terminal_id = "hrd_migration_terminal"
    escalated_id = "hrd_migration_escalated"
    engine = create_engine(database_url, future=True)
    try:
        with engine.begin() as connection:
            for decision_id, decision, review_task_id, status in (
                (terminal_id, "accepted", "hrt_migration_terminal", "success"),
                (escalated_id, "escalated", "hrt_migration_escalated", "escalated"),
            ):
                connection.execute(
                    text(
                        "INSERT INTO human_review_decisions "
                        "(decision_id, tenant_id, project_id, status, trace_id, payload) "
                        "VALUES (:decision_id, :tenant_id, :project_id, :status, "
                        ":trace_id, :payload)"
                    ),
                    {
                        "decision_id": decision_id,
                        "tenant_id": "migration_tenant",
                        "project_id": "migration_project",
                        "status": status,
                        "trace_id": f"trace_{decision_id}",
                        "payload": json.dumps(
                            {
                                "decision_id": decision_id,
                                "review_task_id": review_task_id,
                                "decision": decision,
                            }
                        ),
                    },
                )
        return terminal_id, escalated_id
    finally:
        engine.dispose()


def assert_legacy_human_review_backfilled(
    database_url: str,
    terminal_id: str,
    escalated_id: str,
) -> None:
    engine = create_engine(database_url, future=True)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT decision_id, review_task_id, terminal_review_task_id "
                    "FROM human_review_decisions "
                    "WHERE decision_id IN (:terminal_id, :escalated_id)"
                ),
                {"terminal_id": terminal_id, "escalated_id": escalated_id},
            ).mappings()
            by_id = {str(row["decision_id"]): row for row in rows}
        terminal = by_id[terminal_id]
        escalated = by_id[escalated_id]
        if terminal["review_task_id"] != "hrt_migration_terminal":
            raise AssertionError("terminal review task ID was not backfilled")
        if terminal["terminal_review_task_id"] != "hrt_migration_terminal":
            raise AssertionError("terminal decision key was not backfilled")
        if escalated["review_task_id"] != "hrt_migration_escalated":
            raise AssertionError("escalated review task ID was not backfilled")
        if escalated["terminal_review_task_id"] is not None:
            raise AssertionError("escalated decision must not consume the terminal uniqueness key")
    finally:
        engine.dispose()


def _legacy_calibration_value(decision: str, variant: int) -> object:
    if variant % 5 == 0:
        return decision
    if variant % 5 == 1:
        return {"label": decision}
    if variant % 5 == 2:
        return {"decision": decision}
    if variant % 5 == 3:
        return decision == "pass"
    return {"value": decision, "reason_code": "legacy_reason"}


def insert_legacy_calibration_history(database_url: str) -> None:
    """Insert valid 0018 rows that exercise every 0019 compatibility path."""

    round_vectors = {
        "cal_legacy_nonzero": (
            ["pass", "pass", "fail", "fail"],
            ["pass", "fail", "fail", "fail"],
            500000,
            750000,
        ),
        "cal_legacy_zero_defined": (
            ["pass", "pass", "fail", "fail"],
            ["pass", "fail", "pass", "fail"],
            0,
            500000,
        ),
        "cal_legacy_zero_undefined": (
            ["pass", "pass"],
            ["pass", "pass"],
            0,
            1000000,
        ),
        "cal_legacy_empty_gold": (
            ["pass"],
            ["fail"],
            0,
            0,
        ),
    }
    engine = _causal_test_engine(database_url)
    try:
        with engine.begin() as connection:
            variant = 0
            for round_id, (reviewer_a, reviewer_b, kappa, observed) in round_vectors.items():
                sample_count = len(reviewer_a)
                agreed_count = sum(
                    left == right for left, right in zip(reviewer_a, reviewer_b, strict=True)
                )
                conflict_count = sample_count - agreed_count
                is_empty_gold = round_id == "cal_legacy_empty_gold"
                connection.execute(
                    text(
                        "INSERT INTO calibration_rounds "
                        "(round_id, tenant_id, project_id, dataset_id, dataset_version, "
                        "label_version, rubric_version, sample_manifest_sha256, "
                        "reviewer_a_id, reviewer_b_id, adjudicator_id, status, "
                        "resource_version, sample_count, paired_submission_count, "
                        "agreed_count, conflict_count, adjudication_count, excluded_count, "
                        "observed_agreement_ppm, cohen_kappa_micros, root_trace_id, "
                        "current_trace_id, published_at) VALUES "
                        "(:round_id, 'migration_tenant', 'migration_project', "
                        "'legacy_dataset', 'legacy-v1', 'legacy-label-v1', "
                        "'rubric_quote_risk_v3', :manifest, 'legacy_reviewer_a', "
                        "'legacy_reviewer_b', 'legacy_adjudicator', :status, 2, "
                        ":sample_count, :sample_count, :agreed_count, :conflict_count, "
                        ":adjudication_count, :excluded_count, :observed, :kappa, "
                        ":root_trace, :current_trace, "
                        "CASE WHEN :status = 'published' THEN CURRENT_TIMESTAMP ELSE NULL END)"
                    ),
                    {
                        "round_id": round_id,
                        "manifest": hashlib.sha256(round_id.encode()).hexdigest(),
                        "status": "published" if is_empty_gold else "in_review",
                        "sample_count": sample_count,
                        "agreed_count": agreed_count,
                        "conflict_count": conflict_count,
                        "adjudication_count": conflict_count if is_empty_gold else 0,
                        "excluded_count": 1 if is_empty_gold else 0,
                        "observed": observed,
                        "kappa": kappa,
                        "root_trace": f"trace_root_{round_id}",
                        "current_trace": f"trace_current_{round_id}",
                    },
                )

                for ordinal, (decision_a, decision_b) in enumerate(
                    zip(reviewer_a, reviewer_b, strict=True)
                ):
                    item_id = f"item_{round_id}_{ordinal}"
                    is_excluded = is_empty_gold and ordinal == 0
                    connection.execute(
                        text(
                            "INSERT INTO calibration_items "
                            "(item_id, tenant_id, project_id, round_id, ordinal, "
                            "evidence_ref, source_case_id, status, review_outcome, "
                            "resource_version, trace_id) VALUES "
                            "(:item_id, 'migration_tenant', 'migration_project', :round_id, "
                            ":ordinal, :evidence_ref, :source_case_id, :status, "
                            ":review_outcome, 2, :trace_id)"
                        ),
                        {
                            "item_id": item_id,
                            "round_id": round_id,
                            "ordinal": ordinal,
                            "evidence_ref": f"evidence://migration/{round_id}/{ordinal}",
                            "source_case_id": f"case_{round_id}_{ordinal}",
                            "status": "excluded" if is_excluded else "pending",
                            "review_outcome": "conflicted" if is_excluded else "pending",
                            "trace_id": f"trace_{item_id}",
                        },
                    )
                    for slot, reviewer_id, decision in (
                        ("A", "legacy_reviewer_a", decision_a),
                        ("B", "legacy_reviewer_b", decision_b),
                    ):
                        assignment_id = f"assign_{round_id}_{ordinal}_{slot.lower()}"
                        review_task_id = f"hrt_{round_id}_{ordinal}_{slot.lower()}"
                        submission_id = f"sub_{round_id}_{ordinal}_{slot.lower()}"
                        connection.execute(
                            text(
                                "INSERT INTO human_review_tasks "
                                "(review_task_id, tenant_id, project_id, status, trace_id, "
                                "payload) VALUES (:review_task_id, 'migration_tenant', "
                                "'migration_project', 'success', :trace_id, :payload)"
                            ),
                            {
                                "review_task_id": review_task_id,
                                "trace_id": f"trace_{review_task_id}",
                                "payload": json.dumps(
                                    {"queue": "blind_calibration", "legacy": True}
                                ),
                            },
                        )
                        connection.execute(
                            text(
                                "INSERT INTO calibration_assignments "
                                "(assignment_id, tenant_id, project_id, round_id, item_id, "
                                "slot, reviewer_id, review_task_id, status, "
                                "resource_version, submitted_at, trace_id) VALUES "
                                "(:assignment_id, 'migration_tenant', 'migration_project', "
                                ":round_id, :item_id, :slot, :reviewer_id, "
                                ":review_task_id, 'submitted', 2, CURRENT_TIMESTAMP, "
                                ":trace_id)"
                            ),
                            {
                                "assignment_id": assignment_id,
                                "round_id": round_id,
                                "item_id": item_id,
                                "slot": slot,
                                "reviewer_id": reviewer_id,
                                "review_task_id": review_task_id,
                                "trace_id": f"trace_{assignment_id}",
                            },
                        )
                        legacy_value = _legacy_calibration_value(decision, variant)
                        variant += 1
                        legacy_json = json.dumps(
                            legacy_value,
                            ensure_ascii=True,
                            separators=(",", ":"),
                            sort_keys=True,
                        )
                        connection.execute(
                            text(
                                "INSERT INTO calibration_submissions "
                                "(submission_id, tenant_id, project_id, round_id, item_id, "
                                "assignment_id, reviewer_id, value_json, "
                                "canonical_value_sha256, trace_id) VALUES "
                                "(:submission_id, 'migration_tenant', 'migration_project', "
                                ":round_id, :item_id, :assignment_id, :reviewer_id, "
                                ":value_json, :value_sha256, :trace_id)"
                            ),
                            {
                                "submission_id": submission_id,
                                "round_id": round_id,
                                "item_id": item_id,
                                "assignment_id": assignment_id,
                                "reviewer_id": reviewer_id,
                                "value_json": legacy_json,
                                "value_sha256": hashlib.sha256(
                                    legacy_json.encode("utf-8")
                                ).hexdigest(),
                                "trace_id": f"trace_{submission_id}",
                            },
                        )

            connection.execute(
                text(
                    "INSERT INTO gold_set_versions "
                    "(gold_set_version_id, tenant_id, project_id, round_id, gold_set_key, "
                    "version_number, dataset_id, dataset_version, label_version, "
                    "rubric_version, sample_manifest_sha256, annotation_manifest_sha256, "
                    "sample_count, annotation_count, excluded_count, "
                    "observed_agreement_ppm, cohen_kappa_micros, conflict_count, "
                    "adjudication_count, published_by, trace_id) VALUES "
                    "('gold_legacy_empty', 'migration_tenant', 'migration_project', "
                    "'cal_legacy_empty_gold', 'legacy-empty-gold', 1, 'legacy_dataset', "
                    "'legacy-v1', 'legacy-label-v1', 'rubric_quote_risk_v3', :manifest, "
                    ":annotation_manifest, 1, 0, 1, 0, 0, 1, 1, "
                    "'legacy_adjudicator', 'trace_gold_legacy_empty')"
                ),
                {
                    "manifest": hashlib.sha256(b"cal_legacy_empty_gold").hexdigest(),
                    "annotation_manifest": hashlib.sha256(b"empty_annotations").hexdigest(),
                },
            )
    finally:
        engine.dispose()


def _decoded_json(value: object) -> object:
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        return json.loads(value)
    return value


def assert_legacy_calibration_backfilled(database_url: str) -> None:
    engine = _causal_test_engine(database_url)
    try:
        with engine.connect() as connection:
            round_rows = connection.execute(
                text(
                    "SELECT round_id, cohen_kappa_defined FROM calibration_rounds "
                    "WHERE round_id LIKE 'cal_legacy_%'"
                )
            ).mappings()
            flags = {str(row["round_id"]): bool(row["cohen_kappa_defined"]) for row in round_rows}
            expected_flags = {
                "cal_legacy_nonzero": True,
                "cal_legacy_zero_defined": True,
                "cal_legacy_zero_undefined": False,
                "cal_legacy_empty_gold": True,
            }
            if flags != expected_flags:
                raise AssertionError(
                    f"legacy Cohen kappa flags were not backfilled correctly: {flags!r}"
                )

            submission_rows = (
                connection.execute(
                    text(
                        "SELECT submission_id, value_json, legacy_value_json, "
                        "value_schema_version, canonical_value_sha256 "
                        "FROM calibration_submissions WHERE submission_id LIKE 'sub_cal_legacy_%'"
                    )
                )
                .mappings()
                .all()
            )
            if len(submission_rows) != 22:
                raise AssertionError(
                    f"expected 22 legacy submissions, found {len(submission_rows)}"
                )
            for row in submission_rows:
                value = _decoded_json(row["value_json"])
                if not isinstance(value, dict) or value.get("decision") not in {"pass", "fail"}:
                    raise AssertionError(
                        f"legacy submission was not normalized: {row['submission_id']}"
                    )
                if set(value) != {"decision", "reason_code", "evidence_refs"}:
                    raise AssertionError(
                        f"legacy submission schema mismatch: {row['submission_id']}"
                    )
                canonical = json.dumps(
                    value,
                    allow_nan=False,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                expected_sha = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
                if row["canonical_value_sha256"] != expected_sha:
                    raise AssertionError(f"legacy submission hash mismatch: {row['submission_id']}")
                if int(row["value_schema_version"]) != 1:
                    raise AssertionError(
                        f"legacy submission schema version mismatch: {row['submission_id']}"
                    )
                if row["legacy_value_json"] is None:
                    raise AssertionError(
                        f"legacy submission original value was not retained: {row['submission_id']}"
                    )

            gold = (
                connection.execute(
                    text(
                        "SELECT annotation_count, cohen_kappa_defined, "
                        "legacy_empty_compatible FROM gold_set_versions "
                        "WHERE gold_set_version_id = 'gold_legacy_empty'"
                    )
                )
                .mappings()
                .one()
            )
            if int(gold["annotation_count"]) != 0:
                raise AssertionError("legacy zero-annotation gold version was altered")
            if not bool(gold["cohen_kappa_defined"]):
                raise AssertionError("legacy zero-annotation gold kappa flag was not backfilled")
            if not bool(gold["legacy_empty_compatible"]):
                raise AssertionError(
                    "legacy zero-annotation gold version was not marked compatible"
                )

            next_version = connection.scalar(
                text(
                    "SELECT next_version FROM gold_set_series WHERE tenant_id = "
                    "'migration_tenant' AND project_id = 'migration_project' AND "
                    "gold_set_key = 'legacy-empty-gold'"
                )
            )
            if int(next_version or 0) != 2:
                raise AssertionError("legacy gold series next_version was not backfilled")
    finally:
        engine.dispose()


def insert_legacy_hotword_badcase(database_url: str) -> str:
    badcase_id = "badcase_legacy_hotword_migration"
    engine = create_engine(database_url, future=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO badcases "
                    "(badcase_id, tenant_id, project_id, status, trace_id, payload) "
                    "VALUES (:badcase_id, 'migration_tenant', 'migration_project', "
                    "'pending-attribution', 'trace_legacy_hotword_migration', :payload)"
                ),
                {"badcase_id": badcase_id, "payload": json.dumps({"legacy": True})},
            )
        return badcase_id
    finally:
        engine.dispose()


def assert_legacy_hotword_badcase_backfilled(database_url: str, badcase_id: str) -> None:
    engine = create_engine(database_url, future=True)
    try:
        with engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT root_trace_id, current_trace_id, downstream_impact, "
                        "expected_count, correct_count, weighted_error_count, "
                        "manual_correction_count, priority_score, candidate_state, "
                        "resource_version FROM badcases WHERE badcase_id = :badcase_id"
                    ),
                    {"badcase_id": badcase_id},
                )
                .mappings()
                .one()
            )
        if row["root_trace_id"] != "trace_legacy_hotword_migration":
            raise AssertionError("legacy hotword badcase root trace was not backfilled")
        if row["current_trace_id"] != "trace_legacy_hotword_migration":
            raise AssertionError("legacy hotword badcase current trace was not backfilled")
        if _decoded_json(row["downstream_impact"]) != {}:
            raise AssertionError("legacy hotword badcase downstream impact was not backfilled")
        expected_defaults = {
            "expected_count": 0,
            "correct_count": 0,
            "weighted_error_count": 0.0,
            "manual_correction_count": 0,
            "priority_score": 0.0,
            "candidate_state": "suspected",
            "resource_version": 1,
        }
        for column_name, expected in expected_defaults.items():
            if row[column_name] != expected:
                raise AssertionError(
                    f"legacy hotword badcase default mismatch for {column_name}: "
                    f"{row[column_name]!r}"
                )
    finally:
        engine.dispose()


def _causal_test_engine(database_url: str) -> Engine:
    engine = create_engine(database_url, future=True)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _enable_sqlite_foreign_keys(
            dbapi_connection: Any,
            _connection_record: Any,
        ) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def _expect_integrity_error(
    engine: Engine,
    statement: TextClause,
    parameters: dict[str, object],
    label: str,
) -> None:
    try:
        with engine.begin() as connection:
            connection.execute(statement, parameters)
    except IntegrityError:
        return
    except DBAPIError as exc:
        # MySQL 8 reports CHECK constraint violations as OperationalError 3819
        # through PyMySQL instead of SQLAlchemy's IntegrityError wrapper.
        error_args = getattr(exc.orig, "args", ())
        if engine.dialect.name == "mysql" and error_args:
            if error_args[0] == 3819:
                return
            if (
                error_args[0] == 1644
                and label.startswith("append-only ")
                and "append-only calibration record" in str(error_args[1])
            ):
                return
        raise
    raise AssertionError(f"causal integrity check unexpectedly accepted {label}")


def assert_hotword_enforcement(database_url: str, legacy_badcase_id: str) -> None:
    engine = _causal_test_engine(database_url)
    payload = json.dumps({"migration_probe": True})
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO hotword_packs "
                    "(pack_id, tenant_id, project_id, name, language, domain, status, "
                    "resource_version, root_trace_id, current_trace_id) VALUES "
                    "('pack_hotword_migration', 'migration_tenant', 'migration_project', "
                    "'Migration hotword pack', 'zh-CN', 'migration', 'active', 1, "
                    "'trace_hotword_migration', 'trace_hotword_migration')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO hotword_pack_versions "
                    "(version_id, tenant_id, project_id, pack_id, version, status, "
                    "eval_locked, resource_version, root_trace_id, current_trace_id, payload) "
                    "VALUES ('hwpv_migration_v1', 'migration_tenant', 'migration_project', "
                    "'pack_hotword_migration', 'v1', 'draft', :eval_locked, 1, "
                    "'trace_hotword_migration', 'trace_hotword_migration', :payload)"
                ),
                {"eval_locked": False, "payload": payload},
            )
            connection.execute(
                text(
                    "UPDATE badcases SET capability = 'asr-hotword', "
                    "error_type = 'misrecognition', "
                    "hotword_pack_version_id = 'hwpv_migration_v1' "
                    "WHERE badcase_id = :badcase_id"
                ),
                {"badcase_id": legacy_badcase_id},
            )
            connection.execute(
                text(
                    "INSERT INTO hotword_version_items "
                    "(item_id, tenant_id, project_id, version_id, canonical_term, "
                    "normalized_term, aliases, category, weight, source_badcase_id, "
                    "source_type, resource_version, root_trace_id, current_trace_id) VALUES "
                    "('hotword_item_migration', 'migration_tenant', 'migration_project', "
                    "'hwpv_migration_v1', 'Auris', 'auris', :aliases, 'product', 50, "
                    ":badcase_id, 'badcase', 1, 'trace_hotword_migration', "
                    "'trace_hotword_migration')"
                ),
                {"aliases": json.dumps(["Auris Flow"]), "badcase_id": legacy_badcase_id},
            )
            connection.execute(
                text(
                    "INSERT INTO hotword_metric_snapshots "
                    "(snapshot_id, tenant_id, project_id, bucket_start, bucket_end, "
                    "hotword_pack_version_id, expected_count, correct_count, "
                    "weighted_error_count, false_insert_count, recognized_hotword_count, "
                    "impacted_session_count, evidence_confidence, root_trace_id, payload) "
                    "VALUES ('hotword_metric_migration', 'migration_tenant', "
                    "'migration_project', '2026-07-13 00:00:00', '2026-07-14 00:00:00', "
                    "'hwpv_migration_v1', 3, 2, 1, 0, 2, 1, 1, "
                    "'trace_hotword_migration', :payload)"
                ),
                {"payload": payload},
            )

        _expect_integrity_error(
            engine,
            text(
                "UPDATE hotword_pack_versions SET status = 'production' "
                "WHERE version_id = 'hwpv_migration_v1'"
            ),
            {},
            "invalid hotword version status",
        )
        for invalid_weight in (-1, 101):
            _expect_integrity_error(
                engine,
                text(
                    "UPDATE hotword_version_items SET weight = :weight "
                    "WHERE item_id = 'hotword_item_migration'"
                ),
                {"weight": invalid_weight},
                f"hotword item weight {invalid_weight}",
            )
        _expect_integrity_error(
            engine,
            text("UPDATE badcases SET error_type = 'homophone' WHERE badcase_id = :badcase_id"),
            {"badcase_id": legacy_badcase_id},
            "invalid ASR hotword badcase error_type",
        )
        _expect_integrity_error(
            engine,
            text(
                "UPDATE badcases SET hotword_pack_version_id = 'missing_hotword_version' "
                "WHERE badcase_id = :badcase_id"
            ),
            {"badcase_id": legacy_badcase_id},
            "dangling ASR hotword badcase version",
        )
    finally:
        engine.dispose()


def assert_insight_causal_enforcement(database_url: str) -> None:
    engine = _causal_test_engine(database_url)
    payload = json.dumps({"migration_probe": True})
    try:
        with engine.begin() as connection:
            for run_id, run_type in (
                ("run_causal_report", "insight_report"),
                ("run_causal_eval", "evaluation"),
            ):
                connection.execute(
                    text(
                        "INSERT INTO run_records "
                        "(run_id, tenant_id, project_id, run_type, status, trace_id, payload) "
                        "VALUES (:run_id, 'migration_tenant', 'migration_project', "
                        ":run_type, 'success', :trace_id, :payload)"
                    ),
                    {
                        "run_id": run_id,
                        "run_type": run_type,
                        "trace_id": f"trace_{run_id}",
                        "payload": payload,
                    },
                )
            for metric_result_id in ("metric_causal_baseline", "metric_causal_outcome"):
                connection.execute(
                    text(
                        "INSERT INTO metric_results "
                        "(metric_result_id, tenant_id, project_id, status, trace_id, payload) "
                        "VALUES (:metric_result_id, 'migration_tenant', 'migration_project', "
                        "'snapshot', :trace_id, :payload)"
                    ),
                    {
                        "metric_result_id": metric_result_id,
                        "trace_id": f"trace_{metric_result_id}",
                        "payload": payload,
                    },
                )
            connection.execute(
                text(
                    "INSERT INTO insight_reports "
                    "(report_id, tenant_id, project_id, run_id, status, report_type, "
                    "trace_id, payload) VALUES "
                    "('report_causal', 'migration_tenant', 'migration_project', "
                    "'run_causal_report', 'generated', 'operations', "
                    "'trace_report_causal', :payload)"
                ),
                {"payload": payload},
            )
            connection.execute(
                text(
                    "INSERT INTO insight_actions "
                    "(action_id, tenant_id, project_id, report_id, "
                    "baseline_metric_result_id, action_type, branch, risk_level, status, "
                    "resource_version, trace_id, payload) VALUES "
                    "('action_causal', 'migration_tenant', 'migration_project', "
                    "'report_causal', 'metric_causal_baseline', 'experiment', "
                    "'experiment', 'low', 'experiment_ready', 1, "
                    "'trace_action_causal', :payload)"
                ),
                {"payload": payload},
            )
            connection.execute(
                text(
                    "INSERT INTO insight_experiments "
                    "(experiment_id, tenant_id, project_id, action_id, eval_run_id, "
                    "baseline_metric_result_id, outcome_metric_result_id, status, "
                    "trace_id, payload) VALUES "
                    "('experiment_causal', 'migration_tenant', 'migration_project', "
                    "'action_causal', 'run_causal_eval', 'metric_causal_baseline', "
                    "'metric_causal_outcome', 'measured', 'trace_experiment_causal', :payload)"
                ),
                {"payload": payload},
            )
            connection.execute(
                text(
                    "INSERT INTO insight_effects "
                    "(effect_id, tenant_id, project_id, action_id, experiment_id, "
                    "baseline_metric_result_id, outcome_metric_result_id, metric_key, "
                    "delta, status, trace_id, payload) VALUES "
                    "('effect_causal', 'migration_tenant', 'migration_project', "
                    "'action_causal', 'experiment_causal', 'metric_causal_baseline', "
                    "'metric_causal_outcome', 'conversion_rate', 0.05, 'measured', "
                    "'trace_effect_causal', :payload)"
                ),
                {"payload": payload},
            )

        _expect_integrity_error(
            engine,
            text(
                "INSERT INTO insight_reports "
                "(report_id, tenant_id, project_id, run_id, status, report_type, "
                "trace_id, payload) VALUES "
                "('report_cross_project', 'migration_tenant', 'other_project', "
                "'run_causal_report', 'generated', 'operations', "
                "'trace_cross_project', :payload)"
            ),
            {"payload": payload},
            "cross-project report-to-run reference",
        )
        _expect_integrity_error(
            engine,
            text("DELETE FROM metric_results WHERE metric_result_id = 'metric_causal_baseline'"),
            {},
            "delete of a referenced baseline metric",
        )
        _expect_integrity_error(
            engine,
            text("DELETE FROM insight_reports WHERE report_id = 'report_causal'"),
            {},
            "delete of a referenced insight report",
        )
    finally:
        engine.dispose()


def assert_quality_appeal_enforcement(
    database_url: str,
    terminal_decision_id: str,
    escalated_decision_id: str,
) -> None:
    engine = _causal_test_engine(database_url)
    evidence_refs = json.dumps(["evidence://migration/appeal"])
    task_payload = json.dumps({"queue": "quality_appeal", "migration_probe": True})
    decision_payload = json.dumps(
        {
            "decision": "original_upheld",
            "appeal_id": "qap_migration_valid",
            "supersedes_source_decision_id": terminal_decision_id,
        }
    )
    try:
        with engine.begin() as connection:
            for review_task_id in (
                "hrt_qap_migration_valid",
                "hrt_qap_migration_escalated",
                "hrt_qap_migration_invalid",
            ):
                connection.execute(
                    text(
                        "INSERT INTO human_review_tasks "
                        "(review_task_id, tenant_id, project_id, status, trace_id, payload) "
                        "VALUES (:review_task_id, 'migration_tenant', 'migration_project', "
                        "'submitted', 'trace_quality_appeal_migration', :payload)"
                    ),
                    {"review_task_id": review_task_id, "payload": task_payload},
                )
            connection.execute(
                text(
                    "INSERT INTO human_review_decisions "
                    "(decision_id, tenant_id, project_id, review_task_id, "
                    "terminal_review_task_id, status, trace_id, payload) VALUES "
                    "('hrd_migration_terminal_second', 'migration_tenant', "
                    "'migration_project', 'hrt_migration_terminal_second', "
                    "'hrt_migration_terminal_second', 'success', "
                    "'trace_hrd_migration_terminal_second', :payload)"
                ),
                {
                    "payload": json.dumps(
                        {
                            "decision": "accepted",
                            "review_task_id": "hrt_migration_terminal_second",
                        }
                    )
                },
            )
            connection.execute(
                text(
                    "INSERT INTO quality_appeals "
                    "(appeal_id, tenant_id, project_id, source_decision_id, "
                    "source_review_task_id, review_task_id, source_result_sha256, "
                    "source_decider_id, source_trace_id, root_trace_id, current_trace_id, "
                    "appellant_id, evidence_refs, reason, status, resource_version) VALUES "
                    "('qap_migration_valid', 'migration_tenant', 'migration_project', "
                    ":source_decision_id, 'hrt_migration_terminal', "
                    "'hrt_qap_migration_valid', :source_hash, "
                    "'migration_decider', 'trace_source', 'trace_root', 'trace_current', "
                    "'migration_appellant', :evidence_refs, 'migration probe', "
                    "'submitted', 1)"
                ),
                {
                    "source_decision_id": terminal_decision_id,
                    "source_hash": "a" * 64,
                    "evidence_refs": evidence_refs,
                },
            )

        _expect_integrity_error(
            engine,
            text(
                "INSERT INTO quality_appeals "
                "(appeal_id, tenant_id, project_id, source_decision_id, "
                "source_review_task_id, review_task_id, source_result_sha256, "
                "source_decider_id, source_trace_id, root_trace_id, current_trace_id, "
                "appellant_id, evidence_refs, reason, status, resource_version) VALUES "
                "('qap_migration_escalated', 'migration_tenant', 'migration_project', "
                ":source_decision_id, 'hrt_migration_escalated', "
                "'hrt_qap_migration_escalated', :source_hash, "
                "'migration_decider', 'trace_source', 'trace_root', 'trace_current', "
                "'migration_appellant', :evidence_refs, 'invalid source', "
                "'submitted', 1)"
            ),
            {
                "source_decision_id": escalated_decision_id,
                "source_hash": "b" * 64,
                "evidence_refs": evidence_refs,
            },
            "appeal bound to a non-terminal human review decision",
        )
        _expect_integrity_error(
            engine,
            text(
                "INSERT INTO quality_appeals "
                "(appeal_id, tenant_id, project_id, source_decision_id, "
                "source_review_task_id, review_task_id, source_result_sha256, "
                "source_decider_id, source_trace_id, root_trace_id, current_trace_id, "
                "appellant_id, evidence_refs, reason, status, decision, resource_version) "
                "VALUES "
                "('qap_migration_invalid_state', 'migration_tenant', 'migration_project', "
                "'hrd_migration_terminal_second', 'hrt_migration_terminal_second', "
                "'hrt_qap_migration_invalid', :source_hash, 'migration_decider', "
                "'trace_source', 'trace_root', 'trace_current', 'migration_appellant', "
                ":evidence_refs, 'invalid state', 'resolved', NULL, 1)"
            ),
            {"source_hash": "c" * 64, "evidence_refs": evidence_refs},
            "resolved appeal without an appeal decision",
        )
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO human_review_decisions "
                    "(decision_id, tenant_id, project_id, review_task_id, "
                    "terminal_review_task_id, status, trace_id, payload) VALUES "
                    "('hrd_qap_migration_valid', 'migration_tenant', 'migration_project', "
                    "'hrt_qap_migration_valid', 'hrt_qap_migration_valid', 'resolved', "
                    "'trace_quality_appeal_decision', :payload)"
                ),
                {"payload": decision_payload},
            )
            connection.execute(
                text(
                    "UPDATE quality_appeals SET status = 'resolved', "
                    "decision = 'original_upheld', "
                    "appeal_decision_id = 'hrd_qap_migration_valid', "
                    "resolved_at = CURRENT_TIMESTAMP, resource_version = 2 "
                    "WHERE appeal_id = 'qap_migration_valid'"
                )
            )
        _expect_integrity_error(
            engine,
            text("DELETE FROM human_review_decisions WHERE decision_id = :source_decision_id"),
            {"source_decision_id": terminal_decision_id},
            "delete of a human review decision referenced by an appeal",
        )
        _expect_integrity_error(
            engine,
            text(
                "DELETE FROM human_review_decisions WHERE decision_id = 'hrd_qap_migration_valid'"
            ),
            {},
            "delete of an appeal decision referenced by its appeal",
        )
        _expect_integrity_error(
            engine,
            text("DELETE FROM human_review_tasks WHERE review_task_id = 'hrt_qap_migration_valid'"),
            {},
            "delete of a review task referenced by its appeal",
        )
    finally:
        engine.dispose()


def assert_calibration_enforcement(database_url: str) -> None:
    engine = _causal_test_engine(database_url)
    value_json = json.dumps({"label": "risk"})
    value_sha256 = "a" * 64
    task_payload = json.dumps({"queue": "blind_calibration", "migration_probe": True})
    try:
        with engine.begin() as connection:
            for round_id, sample_count in (
                ("cal_round_migration", 4),
                ("cal_round_other", 1),
            ):
                connection.execute(
                    text(
                        "INSERT INTO calibration_rounds "
                        "(round_id, tenant_id, project_id, dataset_id, dataset_version, "
                        "label_version, rubric_version, sample_manifest_sha256, "
                        "reviewer_a_id, reviewer_b_id, adjudicator_id, sample_count, "
                        "root_trace_id, current_trace_id) VALUES "
                        "(:round_id, 'migration_tenant', 'migration_project', "
                        "'dataset_calibration', 'dataset-v1', 'label-v1', 'rubric-v1', "
                        ":manifest_sha256, 'reviewer_a', 'reviewer_b', 'adjudicator', "
                        ":sample_count, :root_trace_id, :current_trace_id)"
                    ),
                    {
                        "round_id": round_id,
                        "sample_count": sample_count,
                        "manifest_sha256": "b" * 64,
                        "root_trace_id": f"trace_root_{round_id}",
                        "current_trace_id": f"trace_current_{round_id}",
                    },
                )

            for item_id, round_id, ordinal in (
                ("cal_item_one", "cal_round_migration", 0),
                ("cal_item_two", "cal_round_migration", 1),
                ("cal_item_other", "cal_round_other", 0),
            ):
                connection.execute(
                    text(
                        "INSERT INTO calibration_items "
                        "(item_id, tenant_id, project_id, round_id, ordinal, evidence_ref, "
                        "source_case_id, trace_id) VALUES "
                        "(:item_id, 'migration_tenant', 'migration_project', :round_id, "
                        ":ordinal, :evidence_ref, :source_case_id, :trace_id)"
                    ),
                    {
                        "item_id": item_id,
                        "round_id": round_id,
                        "ordinal": ordinal,
                        "evidence_ref": f"evidence://migration/{item_id}",
                        "source_case_id": f"case_{item_id}",
                        "trace_id": f"trace_{item_id}",
                    },
                )

            for task_id in (
                "hrt_cal_item_one_a",
                "hrt_cal_item_one_b",
                "hrt_cal_item_two_a",
            ):
                connection.execute(
                    text(
                        "INSERT INTO human_review_tasks "
                        "(review_task_id, tenant_id, project_id, status, trace_id, payload) "
                        "VALUES (:task_id, 'migration_tenant', 'migration_project', "
                        "'pending', :trace_id, :payload)"
                    ),
                    {
                        "task_id": task_id,
                        "trace_id": f"trace_{task_id}",
                        "payload": task_payload,
                    },
                )

            for assignment_id, item_id, slot, reviewer_id, task_id in (
                (
                    "cal_assignment_one_a",
                    "cal_item_one",
                    "A",
                    "reviewer_a",
                    "hrt_cal_item_one_a",
                ),
                (
                    "cal_assignment_one_b",
                    "cal_item_one",
                    "B",
                    "reviewer_b",
                    "hrt_cal_item_one_b",
                ),
                (
                    "cal_assignment_two_a",
                    "cal_item_two",
                    "A",
                    "reviewer_a",
                    "hrt_cal_item_two_a",
                ),
            ):
                connection.execute(
                    text(
                        "INSERT INTO calibration_assignments "
                        "(assignment_id, tenant_id, project_id, round_id, item_id, slot, "
                        "reviewer_id, review_task_id, trace_id) VALUES "
                        "(:assignment_id, 'migration_tenant', 'migration_project', "
                        "'cal_round_migration', :item_id, :slot, :reviewer_id, :task_id, "
                        ":trace_id)"
                    ),
                    {
                        "assignment_id": assignment_id,
                        "item_id": item_id,
                        "slot": slot,
                        "reviewer_id": reviewer_id,
                        "task_id": task_id,
                        "trace_id": f"trace_{assignment_id}",
                    },
                )

            for submission_id, assignment_id, item_id in (
                ("cal_submission_one_a", "cal_assignment_one_a", "cal_item_one"),
                ("cal_submission_two_a", "cal_assignment_two_a", "cal_item_two"),
            ):
                connection.execute(
                    text(
                        "INSERT INTO calibration_submissions "
                        "(submission_id, tenant_id, project_id, round_id, item_id, "
                        "assignment_id, reviewer_id, value_json, canonical_value_sha256, "
                        "trace_id) VALUES "
                        "(:submission_id, 'migration_tenant', 'migration_project', "
                        "'cal_round_migration', :item_id, :assignment_id, 'reviewer_a', "
                        ":value_json, :value_sha256, :trace_id)"
                    ),
                    {
                        "submission_id": submission_id,
                        "assignment_id": assignment_id,
                        "item_id": item_id,
                        "value_json": value_json,
                        "value_sha256": value_sha256,
                        "trace_id": f"trace_{submission_id}",
                    },
                )

        _expect_integrity_error(
            engine,
            text(
                "INSERT INTO calibration_submissions "
                "(submission_id, tenant_id, project_id, round_id, item_id, assignment_id, "
                "reviewer_id, value_json, canonical_value_sha256, trace_id) VALUES "
                "('cal_submission_wrong_reviewer', 'migration_tenant', 'migration_project', "
                "'cal_round_migration', 'cal_item_one', 'cal_assignment_one_a', "
                "'reviewer_b', :value_json, :value_sha256, 'trace_wrong_reviewer')"
            ),
            {"value_json": value_json, "value_sha256": value_sha256},
            "submission whose reviewer differs from its assignment",
        )
        _expect_integrity_error(
            engine,
            text(
                "INSERT INTO calibration_submissions "
                "(submission_id, tenant_id, project_id, round_id, item_id, assignment_id, "
                "reviewer_id, value_json, canonical_value_sha256, trace_id) VALUES "
                "('cal_submission_duplicate', 'migration_tenant', 'migration_project', "
                "'cal_round_migration', 'cal_item_one', 'cal_assignment_one_a', "
                "'reviewer_a', :value_json, :value_sha256, 'trace_duplicate_submission')"
            ),
            {"value_json": value_json, "value_sha256": value_sha256},
            "second insert-only submission for one assignment",
        )
        _expect_integrity_error(
            engine,
            text(
                "INSERT INTO calibration_adjudications "
                "(adjudication_id, tenant_id, project_id, round_id, item_id, adjudicator_id, "
                "decision, reason, accepted_submission_id, value_json, "
                "canonical_value_sha256, trace_id) VALUES "
                "('cal_adjud_wrong_item', 'migration_tenant', 'migration_project', "
                "'cal_round_migration', 'cal_item_one', 'adjudicator', 'accept_a', "
                "'wrong item probe', 'cal_submission_two_a', :value_json, :value_sha256, "
                "'trace_adjud_wrong_item')"
            ),
            {"value_json": value_json, "value_sha256": value_sha256},
            "adjudication accepting a submission from another item",
        )

        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO calibration_adjudications "
                    "(adjudication_id, tenant_id, project_id, round_id, item_id, "
                    "adjudicator_id, decision, reason, accepted_submission_id, value_json, "
                    "canonical_value_sha256, trace_id) VALUES "
                    "('cal_adjud_one', 'migration_tenant', 'migration_project', "
                    "'cal_round_migration', 'cal_item_one', 'adjudicator', 'accept_a', "
                    "'accepted reviewer A evidence', 'cal_submission_one_a', :value_json, "
                    ":value_sha256, 'trace_cal_adjud_one')"
                ),
                {"value_json": value_json, "value_sha256": value_sha256},
            )

        _expect_integrity_error(
            engine,
            text(
                "INSERT INTO calibration_adjudications "
                "(adjudication_id, tenant_id, project_id, round_id, item_id, adjudicator_id, "
                "decision, reason, value_json, canonical_value_sha256, trace_id) VALUES "
                "('cal_adjud_duplicate', 'migration_tenant', 'migration_project', "
                "'cal_round_migration', 'cal_item_one', 'adjudicator', 'revise', "
                "'duplicate probe', :value_json, :value_sha256, 'trace_adjud_duplicate')"
            ),
            {"value_json": value_json, "value_sha256": value_sha256},
            "second insert-only adjudication for one item",
        )
        _expect_integrity_error(
            engine,
            text(
                "UPDATE calibration_rounds SET paired_submission_count = 1, "
                "agreed_count = 1, conflict_count = 1 "
                "WHERE round_id = 'cal_round_other'"
            ),
            {},
            "round metrics whose agreed and conflict counts do not equal paired count",
        )

        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE calibration_rounds SET status = 'published', "
                    "paired_submission_count = 4, agreed_count = 3, conflict_count = 1, "
                    "adjudication_count = 1, observed_agreement_ppm = 750000, "
                    "cohen_kappa_micros = 500000, cohen_kappa_defined = 1, "
                    "published_at = CURRENT_TIMESTAMP, "
                    "resource_version = 2 WHERE round_id = 'cal_round_migration'"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO gold_set_versions "
                    "(gold_set_version_id, tenant_id, project_id, round_id, gold_set_key, "
                    "version_number, dataset_id, dataset_version, label_version, "
                    "rubric_version, sample_manifest_sha256, annotation_manifest_sha256, "
                    "sample_count, annotation_count, excluded_count, "
                    "observed_agreement_ppm, cohen_kappa_micros, "
                    "cohen_kappa_defined, conflict_count, "
                    "adjudication_count, published_by, trace_id) VALUES "
                    "('gold_version_migration', 'migration_tenant', 'migration_project', "
                    "'cal_round_migration', 'gold-calibration', 1, 'dataset_calibration', "
                    "'dataset-v1', 'label-v1', 'rubric-v1', :sample_manifest_sha256, "
                    ":annotation_manifest_sha256, 4, 4, 0, 750000, 500000, 1, 1, 1, "
                    "'publisher', 'trace_gold_version_migration')"
                ),
                {
                    "sample_manifest_sha256": "b" * 64,
                    "annotation_manifest_sha256": "c" * 64,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO gold_annotations "
                    "(gold_annotation_id, tenant_id, project_id, gold_set_version_id, "
                    "round_id, item_id, source_case_id, evidence_ref, value_json, "
                    "canonical_value_sha256, resolution_source, trace_id) VALUES "
                    "('gold_annotation_one', 'migration_tenant', 'migration_project', "
                    "'gold_version_migration', 'cal_round_migration', 'cal_item_one', "
                    "'case_cal_item_one', 'evidence://migration/cal_item_one', :value_json, "
                    ":value_sha256, 'adjudicated', 'trace_gold_annotation_one')"
                ),
                {"value_json": value_json, "value_sha256": value_sha256},
            )

        _expect_integrity_error(
            engine,
            text(
                "INSERT INTO gold_annotations "
                "(gold_annotation_id, tenant_id, project_id, gold_set_version_id, round_id, "
                "item_id, source_case_id, evidence_ref, value_json, canonical_value_sha256, "
                "resolution_source, trace_id) VALUES "
                "('gold_annotation_wrong_round', 'migration_tenant', 'migration_project', "
                "'gold_version_migration', 'cal_round_other', 'cal_item_other', "
                "'case_cal_item_other', 'evidence://migration/cal_item_other', :value_json, "
                ":value_sha256, 'agreed', 'trace_gold_annotation_wrong_round')"
            ),
            {"value_json": value_json, "value_sha256": value_sha256},
            "gold annotation bound to an item from another round",
        )
        _expect_integrity_error(
            engine,
            text(
                "INSERT INTO gold_annotations "
                "(gold_annotation_id, tenant_id, project_id, gold_set_version_id, round_id, "
                "item_id, source_case_id, evidence_ref, value_json, canonical_value_sha256, "
                "resolution_source, trace_id) VALUES "
                "('gold_annotation_duplicate', 'migration_tenant', 'migration_project', "
                "'gold_version_migration', 'cal_round_migration', 'cal_item_one', "
                "'case_cal_item_one', 'evidence://migration/cal_item_one', :value_json, "
                ":value_sha256, 'adjudicated', 'trace_gold_annotation_duplicate')"
            ),
            {"value_json": value_json, "value_sha256": value_sha256},
            "second insert-only gold annotation for one version and item",
        )
        for table_name, id_column, record_id in (
            ("calibration_submissions", "submission_id", "cal_submission_one_a"),
            ("calibration_adjudications", "adjudication_id", "cal_adjud_one"),
            ("gold_set_versions", "gold_set_version_id", "gold_version_migration"),
            ("gold_annotations", "gold_annotation_id", "gold_annotation_one"),
        ):
            _expect_integrity_error(
                engine,
                text(
                    f"UPDATE {table_name} SET trace_id = 'tampered' WHERE {id_column} = :record_id"
                ),
                {"record_id": record_id},
                f"append-only update of {table_name}",
            )
        _expect_integrity_error(
            engine,
            text("DELETE FROM gold_annotations WHERE gold_annotation_id = 'gold_annotation_one'"),
            {},
            "append-only delete of gold annotation",
        )
    finally:
        engine.dispose()


def assert_tables_removed(database_url: str) -> None:
    engine = create_engine(database_url, future=True)
    try:
        tables = set(inspect(engine).get_table_names())
        remaining = sorted(
            (
                CORE_TABLES
                | DOMAIN_BASELINE_TABLES
                | CALIBRATION_TABLES
                | HOTWORD_TABLES
                | LABEL_EVAL_TABLES
                | LABEL_LIFECYCLE_TABLES
            )
            & tables
        )
        if remaining:
            raise AssertionError(f"downgrade left tables behind: {', '.join(remaining)}")
    finally:
        engine.dispose()


def assert_hotword_revision_removed(database_url: str, legacy_badcase_id: str) -> None:
    engine = create_engine(database_url, future=True)
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        remaining_hotword_tables = sorted(HOTWORD_TABLES & tables)
        if remaining_hotword_tables:
            raise AssertionError(
                "0021 downgrade left hotword tables behind: " + ", ".join(remaining_hotword_tables)
            )
        if "badcases" not in tables:
            raise AssertionError("0021 downgrade removed the pre-existing badcases table")

        badcase_columns = {column["name"] for column in inspector.get_columns("badcases")}
        remaining_columns = sorted(HOTWORD_BADCASE_COLUMNS & badcase_columns)
        if remaining_columns:
            raise AssertionError(
                "0021 downgrade left ASR hotword badcase columns behind: "
                + ", ".join(remaining_columns)
            )
        hotword_index_names = set(HOTWORD_INDEXES["badcases"])
        remaining_indexes = sorted(
            hotword_index_names & {index.get("name") for index in inspector.get_indexes("badcases")}
        )
        if remaining_indexes:
            raise AssertionError(
                "0021 downgrade left ASR hotword badcase indexes behind: "
                + ", ".join(remaining_indexes)
            )

        with engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT status, trace_id, payload FROM badcases "
                        "WHERE badcase_id = :badcase_id"
                    ),
                    {"badcase_id": legacy_badcase_id},
                )
                .mappings()
                .one()
            )
        if row["status"] != "pending-attribution":
            raise AssertionError("0021 downgrade altered the legacy badcase status")
        if row["trace_id"] != "trace_legacy_hotword_migration":
            raise AssertionError("0021 downgrade altered the legacy badcase trace")
        if _decoded_json(row["payload"]) != {"legacy": True}:
            raise AssertionError("0021 downgrade altered the legacy badcase payload")
    finally:
        engine.dispose()


def run_migration_cycle(database_url: str) -> None:
    os.environ["DATABASE_URL"] = database_url
    assert_hotword_seed_references(ROOT / "doc" / "backend-spec" / "seed-fixture-v0.1.json")
    config = alembic_config()
    command.upgrade(config, "0008_storage_objects_table")
    legacy_event_id = insert_legacy_outbox_event(database_url)
    command.upgrade(config, "0012_insight_action_closure")
    terminal_id, escalated_id = insert_legacy_human_review_decisions(database_url)
    command.upgrade(config, "0018_blind_calibration_gold_loop")
    insert_legacy_calibration_history(database_url)
    command.upgrade(config, "0020_auth_sessions")
    legacy_hotword_badcase_id = insert_legacy_hotword_badcase(database_url)
    command.upgrade(config, "head")
    assert_tables_present(database_url)
    assert_legacy_hotword_badcase_backfilled(database_url, legacy_hotword_badcase_id)
    assert_hotword_enforcement(database_url, legacy_hotword_badcase_id)
    assert_insight_causal_enforcement(database_url)
    assert_quality_appeal_enforcement(database_url, terminal_id, escalated_id)
    assert_calibration_enforcement(database_url)
    assert_legacy_outbox_backfilled(database_url, legacy_event_id)
    assert_legacy_human_review_backfilled(database_url, terminal_id, escalated_id)
    assert_legacy_calibration_backfilled(database_url)

    command.upgrade(config, "head")
    assert_tables_present(database_url)

    command.downgrade(config, "0020_auth_sessions")
    assert_hotword_revision_removed(database_url, legacy_hotword_badcase_id)
    command.upgrade(config, "head")
    assert_tables_present(database_url)
    assert_legacy_hotword_badcase_backfilled(database_url, legacy_hotword_badcase_id)

    command.downgrade(config, "base")
    assert_tables_removed(database_url)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify Alembic upgrade, legacy backfill, idempotence and downgrade."
    )
    parser.add_argument(
        "--database-url",
        help=(
            "Explicit disposable database URL. Its schema will be upgraded and downgraded. "
            "Defaults to MIGRATION_DATABASE_URL, then a temporary SQLite database."
        ),
    )
    args = parser.parse_args()
    sys.path.insert(0, str(BACKEND))

    migration_database_url = args.database_url or os.getenv("MIGRATION_DATABASE_URL")
    if migration_database_url:
        run_migration_cycle(migration_database_url)
        print("migration verification ok")
        return

    with TemporaryDirectory(prefix="auris_migration_") as temp_dir:
        db_path = Path(temp_dir) / "migration_check.sqlite"
        database_url = f"sqlite:///{db_path}"
        run_migration_cycle(database_url)

    print("migration verification ok")


if __name__ == "__main__":
    main()
