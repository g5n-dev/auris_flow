from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

from sqlalchemy import and_, func, or_
from sqlalchemy.sql.elements import ColumnElement

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.core.json_keys import (
    build_json_key_aliases,
    json_key_fingerprint,
    normalize_json_key,
)
from app.core.rbac import require_any_role
from app.models import JsonResource

ReadClassification = Literal["standard", "sensitive", "assignment_scoped"]

HUMAN_REVIEW_READ_ROLES = (
    "project_admin",
    "review_arbitrator",
    "annotator",
)

VOICEPRINT_SENSITIVE_READ_ROLES = (
    "project_admin",
    "review_arbitrator",
)


@dataclass(frozen=True)
class ResourceReadPolicy:
    classification: ReadClassification
    roles: tuple[str, ...] = ()


RESOURCE_READ_POLICIES: Mapping[str, ResourceReadPolicy] = MappingProxyType(
    {
        "asr_segments": ResourceReadPolicy("standard"),
        "audio_quality_reports": ResourceReadPolicy("standard"),
        "audio_sessions": ResourceReadPolicy("standard"),
        "connectors": ResourceReadPolicy("standard"),
        "conversation_boundaries": ResourceReadPolicy("standard"),
        "data_aggregation_views": ResourceReadPolicy("standard"),
        "data_assets": ResourceReadPolicy("standard"),
        "data_source_records": ResourceReadPolicy("standard"),
        "documents": ResourceReadPolicy("standard"),
        "eval_datasets": ResourceReadPolicy("standard"),
        "event_links": ResourceReadPolicy("standard"),
        "evidence_packs": ResourceReadPolicy("standard"),
        "human_review_tasks": ResourceReadPolicy(
            "assignment_scoped",
            HUMAN_REVIEW_READ_ROLES,
        ),
        "human_review_decisions": ResourceReadPolicy(
            "sensitive",
            HUMAN_REVIEW_READ_ROLES,
        ),
        "insight_reports": ResourceReadPolicy("standard"),
        "insight_funnels": ResourceReadPolicy("standard"),
        "knowledge_effects": ResourceReadPolicy("standard"),
        "knowledge_indexes": ResourceReadPolicy("standard"),
        "knowledge_quality_gates": ResourceReadPolicy("standard"),
        "knowledge_sources": ResourceReadPolicy("standard"),
        "label_candidates": ResourceReadPolicy("standard"),
        "label_aggregates": ResourceReadPolicy("standard"),
        "label_taxonomy_suggestions": ResourceReadPolicy(
            "sensitive",
            ("project_admin", "model_engineer", "review_arbitrator"),
        ),
        "label_versions": ResourceReadPolicy("standard"),
        "prompt_version_candidates": ResourceReadPolicy(
            "sensitive",
            ("project_admin", "model_engineer", "review_arbitrator"),
        ),
        "listening_annotations": ResourceReadPolicy("standard"),
        "platform_sessions": ResourceReadPolicy(
            "sensitive",
            ("project_admin", "asset_manager"),
        ),
        "recordings": ResourceReadPolicy("standard"),
        "settings": ResourceReadPolicy(
            "sensitive",
            ("project_admin", "model_engineer"),
        ),
        "settings_drafts": ResourceReadPolicy(
            "sensitive",
            ("project_admin", "model_engineer"),
        ),
        "speaker_turns": ResourceReadPolicy("standard"),
        "task_types": ResourceReadPolicy("standard"),
        "task_versions": ResourceReadPolicy("standard"),
        "taxonomies": ResourceReadPolicy("standard"),
        "vad_segments": ResourceReadPolicy("standard"),
        "voiceprint_enrollments": ResourceReadPolicy(
            "sensitive",
            VOICEPRINT_SENSITIVE_READ_ROLES,
        ),
        "voiceprint_samples": ResourceReadPolicy(
            "sensitive",
            VOICEPRINT_SENSITIVE_READ_ROLES,
        ),
        "work_items": ResourceReadPolicy("standard"),
    }
)

# Strong-table trace subjects are not generic JsonResource collections. Keep
# their visibility policy separate so registering an audit/outbox object type
# cannot accidentally expose a new runtime projection collection.
TRACE_REFERENCE_READ_POLICIES: Mapping[str, ResourceReadPolicy] = MappingProxyType(
    {
        "label_recompute_runs": ResourceReadPolicy(
            "sensitive",
            ("project_admin", "model_engineer"),
        ),
        "oidc_identities": ResourceReadPolicy(
            "sensitive",
            ("project_admin",),
        ),
    }
)

TRACE_READ_ROLES = (
    "project_admin",
    "asset_manager",
    "model_engineer",
    "review_arbitrator",
)

HUMAN_REVIEW_ASSIGNMENT_FIELDS = (
    "assignee_id",
    "assignee_user_id",
    "assigned_to",
    "reviewer_id",
)

TRACE_REFERENCE_TYPE_FIELDS = (
    "collection",
    "type",
    "ref_type",
    "resource_type",
    "aggregate_type",
    "object_type",
    "subject_type",
)

TRACE_REFERENCE_ID_FIELDS = (
    "id",
    "ref_id",
    "resource_id",
    "aggregate_id",
    "object_id",
    "subject_id",
)

TRACE_REFERENCE_COLLECTION_ALIASES: Mapping[str, str] = MappingProxyType(
    {
        "agent_result": "work_items",
        "aggregation_policy_version": "label_versions",
        "asset_materialization": "data_assets",
        "asset_backfill": "data_assets",
        "asset_check_retry": "data_assets",
        "asr_segment": "asr_segments",
        "asr_annotation_correction": "listening_annotations",
        "audio_ingest": "audio_sessions",
        "audio_intelligence": "audio_sessions",
        "audio_quality_report": "audio_quality_reports",
        "audio_recording": "recordings",
        "audio_session": "audio_sessions",
        "audio_url": "recordings",
        "badcase": "work_items",
        "badcase_candidate": "work_items",
        "boundary_sync": "conversation_boundaries",
        "calibration_assignment": "work_items",
        "calibration_item": "work_items",
        "calibration_round": "work_items",
        "conversation_boundary": "conversation_boundaries",
        "connector": "connectors",
        "closed_loop_review_adjudication": "label_versions",
        "closed_loop_review_submission": "label_versions",
        "controlled_experiment": "task_versions",
        "data_aggregation_view": "data_aggregation_views",
        "data_asset": "data_assets",
        "document": "documents",
        "employee": "work_items",
        "eval_dataset": "eval_datasets",
        "eval_dataset_version": "eval_datasets",
        "eval_feedback": "eval_datasets",
        "eval_run": "eval_datasets",
        "evaluation": "eval_datasets",
        "experiment_assignment": "task_versions",
        "experiment_exposure": "task_versions",
        "experiment_metric_snapshot": "task_versions",
        "evidence": "evidence_packs",
        "evidence_pack": "evidence_packs",
        "event_link": "event_links",
        "export": "data_assets",
        "external_callback": "work_items",
        "feedback_task": "work_items",
        "feedback_example": "label_aggregates",
        "gold_set_version": "work_items",
        "hotword_analysis": "settings",
        "hotword_build": "settings",
        "hotword_eval": "settings",
        "hotword_metrics": "settings",
        "hotword_pack": "settings",
        "hotword_pack_version": "settings",
        "hotword_publish": "settings",
        "hotword_version_item": "settings",
        "human_review_task": "human_review_tasks",
        "human_review_tasks": "human_review_tasks",
        "review_task": "human_review_tasks",
        "human_review_decision": "human_review_decisions",
        "human_review_decisions": "human_review_decisions",
        "human_review_decision_batch": "work_items",
        "review_decision": "human_review_decisions",
        "review_decisions": "human_review_decisions",
        "insight_action": "insight_reports",
        "insight_experiment": "insight_reports",
        "insight_metric_aggregation": "insight_reports",
        "insight_report": "insight_reports",
        "insight_report_metric_binding": "insight_reports",
        "knowledge_build": "knowledge_indexes",
        "knowledge_effect": "knowledge_effects",
        "knowledge_index": "knowledge_indexes",
        "knowledge_quality_gate": "knowledge_quality_gates",
        "knowledge_source": "knowledge_sources",
        "knowledge_sync": "knowledge_sources",
        "labeling_gold": "eval_datasets",
        "label_candidate": "label_candidates",
        "label_aggregate": "label_aggregates",
        "label_aggregates": "label_aggregates",
        "label_aggregation_policy": "label_versions",
        "label_aggregation_run": "label_aggregates",
        "label_calibration_version": "label_versions",
        "label_conflict": "label_versions",
        "label_eval_result": "eval_datasets",
        "label_extraction_run": "label_aggregates",
        "label_fact": "label_aggregates",
        "label_fact_backfill": "label_aggregates",
        "label_fact_set": "label_aggregates",
        "label_fact_set_head": "label_aggregates",
        "label_fact_set_head_event": "label_aggregates",
        "label_lifecycle_backfill_run": "label_versions",
        "label_mapping_bundle": "label_versions",
        "label_mapping_version": "label_versions",
        "label_node": "label_versions",
        "label_observation": "label_aggregates",
        "label_optimization": "label_versions",
        "label_optimization_metric_snapshot": "label_versions",
        "label_optimization_round": "label_versions",
        "label_optimization_schedule": "label_versions",
        "label_optimization_trigger_scan": "label_versions",
        "label_policy_version": "label_versions",
        "label_publish": "label_versions",
        "label_recompute_run": "label_recompute_runs",
        "label_recompute_run_item": "label_recompute_runs",
        "label_version": "label_versions",
        "label_versions": "label_versions",
        "oidc_identity": "oidc_identities",
        "label_taxonomy_suggestion": "label_taxonomy_suggestions",
        "label_version_deprecation_preflight": "label_versions",
        "taxonomy_suggestion": "label_taxonomy_suggestions",
        "listening_annotation": "listening_annotations",
        "metric_aggregation": "insight_reports",
        "metric_definition": "insight_reports",
        "metric_result": "insight_reports",
        "metric_snapshot": "settings",
        "model_capability": "settings",
        "open_trace": "work_items",
        "platform_auth": "platform_sessions",
        "platform_session": "platform_sessions",
        "platform_sync": "work_items",
        "project": "work_items",
        "project_scene_profile_binding": "task_types",
        "projects": "work_items",
        "prompt_version": "work_items",
        "prompt_asset": "prompt_version_candidates",
        "prompt_candidate": "prompt_version_candidates",
        "prompt_review_adjudication": "prompt_version_candidates",
        "prompt_review_submission": "prompt_version_candidates",
        "prompt_version_candidate": "prompt_version_candidates",
        "prompt_version_candidates": "prompt_version_candidates",
        "prompt_regression": "eval_datasets",
        "provider_test": "settings",
        "quality_appeal": "work_items",
        "recording": "recordings",
        "release_bundle_head": "task_versions",
        "release_bundle_head_event": "task_versions",
        "release_command": "task_versions",
        "release_deployment": "task_versions",
        "run_record": "work_items",
        "settings_publish": "settings",
        "setting": "settings",
        "settings_draft": "settings_drafts",
        "scene_profile": "task_types",
        "scene_profile_version": "task_types",
        "speaker_turn": "speaker_turns",
        "storage_object": "data_assets",
        "store": "work_items",
        "task_run": "task_versions",
        "task_run_cancellation": "task_versions",
        "task_run_status_sync": "task_versions",
        "task_type": "task_types",
        "task_version": "task_versions",
        "task_version_publish": "task_versions",
        "task_version_release_head": "task_versions",
        "taxonomy": "taxonomies",
        "tenant": "work_items",
        "tenants": "work_items",
        "test_run": "work_items",
        "trace_context": "work_items",
        "verification_run": "work_items",
        "vad_segment": "vad_segments",
        "voiceprint": "voiceprint_enrollments",
        "voiceprints": "voiceprint_enrollments",
        "voiceprint_embedding": "voiceprint_samples",
        "voiceprint_embeddings": "voiceprint_samples",
        "voiceprint_enrollment": "voiceprint_enrollments",
        "voiceprint_enrollments": "voiceprint_enrollments",
        "voiceprint_profile": "voiceprint_enrollments",
        "voiceprint_record": "voiceprint_enrollments",
        "voiceprint_sample": "voiceprint_samples",
        "voiceprint_samples": "voiceprint_samples",
        "work_item": "work_items",
    }
)

TRACE_REFERENCE_CONTAINER_FIELDS = frozenset(
    {
        "affected_objects",
        "input_refs",
        "reference",
        "references",
        "resource_ref",
        "result_ref",
        "subject",
        "target_refs",
    }
)

TRACE_REFERENCE_KEY_COLLECTIONS: Mapping[str, str] = MappingProxyType(
    {
        "human_review_task_id": "human_review_tasks",
        "review_task_id": "human_review_tasks",
        "source_review_task_id": "human_review_tasks",
        "terminal_review_task_id": "human_review_tasks",
        "human_review_decision_id": "human_review_decisions",
        "review_decision_id": "human_review_decisions",
        "appeal_decision_id": "human_review_decisions",
        "source_decision_id": "human_review_decisions",
        "voiceprint_enrollment_id": "voiceprint_enrollments",
        "enrollment_id": "voiceprint_enrollments",
        "voiceprint_id": "voiceprint_enrollments",
        "voiceprint_sample_id": "voiceprint_samples",
        "voiceprint_embedding_ref": "voiceprint_samples",
        "prompt_candidate_id": "prompt_version_candidates",
    }
)

TRACE_GOVERNED_FIELD_ALIASES = build_json_key_aliases(
    (
        *TRACE_REFERENCE_TYPE_FIELDS,
        *TRACE_REFERENCE_ID_FIELDS,
        *TRACE_REFERENCE_CONTAINER_FIELDS,
        *TRACE_REFERENCE_KEY_COLLECTIONS,
        *RESOURCE_READ_POLICIES,
        *TRACE_REFERENCE_READ_POLICIES,
        *TRACE_REFERENCE_COLLECTION_ALIASES,
    )
)


def trace_reference_ids(value: object, collection: str) -> set[str]:
    """Collect exact structured references so callers can authorize linked rows."""
    references: set[str] = set()
    if isinstance(value, list):
        for item in value:
            references.update(trace_reference_ids(item, collection))
        return references
    if not isinstance(value, Mapping):
        return references

    normalized_value, has_collision = _normalized_trace_mapping(value)
    if has_collision:
        return references
    typed_collection = next(
        (
            resolved
            for field in TRACE_REFERENCE_TYPE_FIELDS
            if (resolved := trace_reference_collection(normalized_value.get(field))) is not None
        ),
        None,
    )
    if typed_collection == collection:
        reference_id = next(
            (
                item
                for field in TRACE_REFERENCE_ID_FIELDS
                if isinstance((item := normalized_value.get(field)), str) and item
            ),
            None,
        )
        if reference_id:
            references.add(reference_id)

    for field, field_collection in TRACE_REFERENCE_KEY_COLLECTIONS.items():
        field_value = normalized_value.get(field)
        if field_collection == collection and isinstance(field_value, str) and field_value:
            references.add(field_value)

    for field, nested in normalized_value.items():
        nested_collection = trace_reference_collection(field)
        if nested_collection != collection or field in TRACE_REFERENCE_TYPE_FIELDS:
            continue
        if isinstance(nested, Mapping):
            nested_id = _trace_reference_id(nested)
            if nested_id:
                references.add(nested_id)
        elif isinstance(nested, str) and nested:
            references.add(nested)

    for nested in normalized_value.values():
        if isinstance(nested, (Mapping, list)):
            references.update(trace_reference_ids(nested, collection))
    return references


def require_resource_read(ctx: RequestContext, collection: str) -> None:
    policy = RESOURCE_READ_POLICIES.get(collection)
    if policy is None:
        raise ApiError(
            "RESOURCE_READ_POLICY_UNREGISTERED",
            "资源读取策略未注册",
            500,
            details=[{"collection": collection}],
        )
    if policy.roles:
        require_any_role(ctx, policy.roles, action=f"{collection}.read")


def can_read_resource_collection(ctx: RequestContext, collection: str) -> bool:
    """Return a fail-closed collection visibility decision for aggregate views."""
    policy = RESOURCE_READ_POLICIES.get(collection)
    if policy is None:
        return False
    return "system" in ctx.roles or not policy.roles or bool(set(ctx.roles) & set(policy.roles))


def readable_resource_collections(ctx: RequestContext) -> tuple[str, ...]:
    """Return registered collections readable by this context for SQL allow-listing."""
    return tuple(
        collection
        for collection in RESOURCE_READ_POLICIES
        if can_read_resource_collection(ctx, collection)
    )


def require_voiceprint_sensitive_read(ctx: RequestContext) -> None:
    require_any_role(
        ctx,
        VOICEPRINT_SENSITIVE_READ_ROLES,
        action="voiceprints.read_sensitive",
    )


def resource_read_scope(
    ctx: RequestContext,
    collection: str,
) -> ColumnElement[bool] | None:
    """Return a database predicate for row-scoped resource collections."""
    policy = RESOURCE_READ_POLICIES.get(collection)
    if policy is None:
        raise ApiError(
            "RESOURCE_READ_POLICY_UNREGISTERED",
            "资源读取策略未注册",
            500,
            details=[{"collection": collection}],
        )
    if policy.classification != "assignment_scoped":
        return None

    assignment_values = tuple(
        func.coalesce(JsonResource.data[field].as_string(), "")
        for field in HUMAN_REVIEW_ASSIGNMENT_FIELDS
    )
    assigned_to_current = or_(*(value == ctx.user_id for value in assignment_values))
    unassigned = and_(*(value == "" for value in assignment_values))
    queue = func.coalesce(JsonResource.data["queue"].as_string(), "")
    blind_scope = and_(queue == "blind_calibration", assigned_to_current)
    if {"project_admin", "review_arbitrator"}.intersection(ctx.roles):
        regular_scope = queue != "blind_calibration"
    else:
        regular_scope = and_(
            queue != "blind_calibration",
            or_(unassigned, assigned_to_current),
        )
    return or_(blind_scope, regular_scope)


def require_trace_read(ctx: RequestContext) -> None:
    require_any_role(ctx, TRACE_READ_ROLES, action="traces.read")


def can_read_human_review_task(task: Mapping[str, Any], ctx: RequestContext) -> bool:
    if not can_read_resource_collection(ctx, "human_review_tasks"):
        return False
    assignee_ids = {
        value
        for field in HUMAN_REVIEW_ASSIGNMENT_FIELDS
        if isinstance((value := task.get(field)), str) and value
    }
    if task.get("queue") == "blind_calibration":
        return ctx.user_id in assignee_ids
    if not assignee_ids or ctx.user_id in assignee_ids:
        return True
    return bool({"project_admin", "review_arbitrator"}.intersection(ctx.roles))


def trace_reference_collection(value: object) -> str | None:
    """Resolve an exact structured trace subject to its governed collection."""
    if not isinstance(value, str):
        return None
    normalized = _normalize_trace_field(value)
    if not normalized:
        return None
    if normalized in RESOURCE_READ_POLICIES or normalized in TRACE_REFERENCE_READ_POLICIES:
        return normalized
    return TRACE_REFERENCE_COLLECTION_ALIASES.get(normalized)


def trace_reference_is_visible(
    value: object,
    ctx: RequestContext,
    *,
    visible_review_task_ids: set[str] | frozenset[str] = frozenset(),
    visible_review_decision_ids: set[str] | frozenset[str] = frozenset(),
) -> bool:
    """Fail closed when nested trace JSON references a governed hidden resource."""
    if isinstance(value, list):
        return all(
            trace_reference_is_visible(
                item,
                ctx,
                visible_review_task_ids=visible_review_task_ids,
                visible_review_decision_ids=visible_review_decision_ids,
            )
            for item in value
        )
    if not isinstance(value, Mapping):
        return True

    normalized_value, has_collision = _normalized_trace_mapping(value)
    if has_collision:
        return bool({"project_admin", "system"}.intersection(ctx.roles))
    reference_id = _trace_reference_id(normalized_value)
    for field in TRACE_REFERENCE_TYPE_FIELDS:
        raw_type = normalized_value.get(field)
        if raw_type in (None, ""):
            continue
        collection = trace_reference_collection(raw_type)
        if collection is None:
            if reference_id:
                return False
            continue
        if not _trace_collection_reference_is_visible(
            collection,
            reference_id,
            ctx,
            visible_review_task_ids=visible_review_task_ids,
            visible_review_decision_ids=visible_review_decision_ids,
        ):
            return False

    for field, field_collection in TRACE_REFERENCE_KEY_COLLECTIONS.items():
        field_value = normalized_value.get(field)
        if field_value in (None, "", [], {}):
            continue
        field_reference_id = field_value if isinstance(field_value, str) else None
        if not _trace_collection_reference_is_visible(
            field_collection,
            field_reference_id,
            ctx,
            visible_review_task_ids=visible_review_task_ids,
            visible_review_decision_ids=visible_review_decision_ids,
        ):
            return False

    for field, nested in normalized_value.items():
        if field in TRACE_REFERENCE_TYPE_FIELDS or field in TRACE_REFERENCE_KEY_COLLECTIONS:
            continue
        nested_collection = trace_reference_collection(field)
        if nested_collection is not None and isinstance(nested, (Mapping, list, str)):
            nested_ids = _nested_reference_ids(nested)
            if not nested_ids:
                nested_ids = {None}
            if any(
                not _trace_collection_reference_is_visible(
                    nested_collection,
                    nested_id,
                    ctx,
                    visible_review_task_ids=visible_review_task_ids,
                    visible_review_decision_ids=visible_review_decision_ids,
                )
                for nested_id in nested_ids
            ):
                return False
        if (
            field in TRACE_REFERENCE_CONTAINER_FIELDS
            and isinstance(nested, Mapping)
            and _trace_reference_id(nested)
            and not any(nested.get(type_field) for type_field in TRACE_REFERENCE_TYPE_FIELDS)
        ):
            return False

    return all(
        trace_reference_is_visible(
            nested,
            ctx,
            visible_review_task_ids=visible_review_task_ids,
            visible_review_decision_ids=visible_review_decision_ids,
        )
        for nested in normalized_value.values()
        if isinstance(nested, (Mapping, list))
    )


def trace_payload_field_names(value: object) -> list[str]:
    """Expose JSON shape without echoing arbitrary trace payload values."""
    if not isinstance(value, Mapping):
        return []
    return sorted(
        {
            _normalize_trace_field(field)
            for field in value
            if isinstance(field, str) and _normalize_trace_field(field)
        }
    )


def _normalize_trace_field(value: str) -> str:
    canonical = TRACE_GOVERNED_FIELD_ALIASES.get(json_key_fingerprint(value))
    return canonical if canonical is not None else normalize_json_key(value)


def _normalized_trace_mapping(value: Mapping[Any, Any]) -> tuple[dict[str, Any], bool]:
    normalized: dict[str, Any] = {}
    for field, nested in value.items():
        if not isinstance(field, str):
            continue
        canonical = _normalize_trace_field(field)
        if not canonical:
            continue
        if canonical in normalized:
            return normalized, True
        normalized[canonical] = nested
    return normalized, False


def _trace_reference_id(value: Mapping[str, Any]) -> str | None:
    return next(
        (
            item
            for field in TRACE_REFERENCE_ID_FIELDS
            if isinstance((item := value.get(field)), str) and item
        ),
        None,
    )


def _nested_reference_ids(value: object) -> set[str | None]:
    if isinstance(value, str):
        return {value} if value else set()
    if isinstance(value, Mapping):
        reference_id = _trace_reference_id(value)
        return {reference_id} if reference_id else set()
    if isinstance(value, list):
        return {
            reference_id
            for item in value
            if isinstance(item, Mapping) and (reference_id := _trace_reference_id(item)) is not None
        }
    return set()


def _trace_collection_reference_is_visible(
    collection: str,
    reference_id: str | None,
    ctx: RequestContext,
    *,
    visible_review_task_ids: set[str] | frozenset[str],
    visible_review_decision_ids: set[str] | frozenset[str],
) -> bool:
    policy = RESOURCE_READ_POLICIES.get(collection) or TRACE_REFERENCE_READ_POLICIES.get(collection)
    if policy is None or (
        "system" not in ctx.roles and policy.roles and not set(ctx.roles).intersection(policy.roles)
    ):
        return False
    if collection == "human_review_tasks":
        return bool(reference_id and reference_id in visible_review_task_ids)
    if collection == "human_review_decisions":
        return bool(reference_id and reference_id in visible_review_decision_ids)
    return True
