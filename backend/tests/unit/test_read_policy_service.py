from __future__ import annotations

import ast
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import mysql

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.models import JsonResource
from app.services.read_policy_service import (
    RESOURCE_READ_POLICIES,
    can_read_human_review_task,
    can_read_resource_collection,
    readable_resource_collections,
    require_resource_read,
    require_voiceprint_sensitive_read,
    resource_read_scope,
    trace_reference_collection,
    trace_reference_ids,
    trace_reference_is_visible,
)


def _context(*roles: str) -> RequestContext:
    return RequestContext(
        tenant_id="aurora_auto",
        project_id="sales_qa",
        user_id="u_read_policy",
        roles=roles,
        request_id="request-read-policy",
        trace_id="trace_read_policy",
    )


def test_registered_standard_resource_allows_project_member() -> None:
    require_resource_read(_context("asset_manager"), "data_assets")


def test_registered_sensitive_resource_requires_declared_role() -> None:
    with pytest.raises(ApiError) as exc_info:
        require_resource_read(_context("annotator"), "settings")

    assert getattr(exc_info.value, "code", None) == "FORBIDDEN"


def test_label_recompute_trace_objects_use_a_sensitive_read_policy() -> None:
    for object_type in ("label_recompute_run", "label_recompute_run_item"):
        assert trace_reference_collection(object_type) == "label_recompute_runs"
        assert not trace_reference_is_visible(
            {"type": object_type, "id": "recompute-hidden"},
            _context("annotator"),
        )
        for role in ("project_admin", "model_engineer", "system"):
            assert trace_reference_is_visible(
                {"type": object_type, "id": "recompute-visible"},
                _context(role),
            )


def test_oidc_identity_trace_object_uses_an_admin_only_sensitive_policy() -> None:
    assert trace_reference_collection("oidc_identity") == "oidc_identities"
    for role in ("annotator", "model_engineer", "asset_manager"):
        assert not trace_reference_is_visible(
            {"type": "oidc_identity", "id": "identity-hidden"},
            _context(role),
        )
    for role in ("project_admin", "system"):
        assert trace_reference_is_visible(
            {"type": "oidc_identity", "id": "identity-visible"},
            _context(role),
        )


def test_new_operational_trace_objects_use_least_privilege_policies() -> None:
    assert trace_reference_collection("browser_auth_session") == "browser_auth_sessions"
    assert trace_reference_collection("qdrant_rebuild_plan") == "qdrant_rebuild_plans"

    for role in ("annotator", "asset_manager", "model_engineer"):
        assert not trace_reference_is_visible(
            {"type": "browser_auth_session", "id": "session-hidden"},
            _context(role),
        )
    for role in ("project_admin", "system"):
        assert trace_reference_is_visible(
            {"type": "browser_auth_session", "id": "session-visible"},
            _context(role),
        )

    for role in ("annotator", "model_engineer", "review_arbitrator"):
        assert not trace_reference_is_visible(
            {"type": "qdrant_rebuild_plan", "id": "plan-hidden"},
            _context(role),
        )
    for role in ("project_admin", "asset_manager", "system"):
        assert trace_reference_is_visible(
            {"type": "qdrant_rebuild_plan", "id": "plan-visible"},
            _context(role),
        )


def test_prompt_candidate_trace_objects_reuse_sensitive_collection_policy() -> None:
    for object_type in (
        "prompt_candidate",
        "prompt_version_candidate",
        "prompt_version_candidates",
    ):
        assert trace_reference_collection(object_type) == "prompt_version_candidates"
        assert not trace_reference_is_visible(
            {"type": object_type, "id": "candidate-hidden"},
            _context("asset_manager"),
        )
        for role in ("project_admin", "model_engineer", "review_arbitrator", "system"):
            assert trace_reference_is_visible(
                {"type": object_type, "id": "candidate-visible"},
                _context(role),
            )

    assert not trace_reference_is_visible(
        {"prompt_candidate_id": "candidate-hidden-by-key"},
        _context("asset_manager"),
    )


def test_voiceprint_sensitive_reads_require_explicit_privileged_role() -> None:
    for role in ("annotator", "model_engineer", "asset_manager"):
        with pytest.raises(ApiError) as exc_info:
            require_voiceprint_sensitive_read(_context(role))
        assert exc_info.value.code == "FORBIDDEN"
        assert not can_read_resource_collection(_context(role), "voiceprint_enrollments")

    for role in ("project_admin", "review_arbitrator", "system"):
        require_voiceprint_sensitive_read(_context(role))
        assert can_read_resource_collection(_context(role), "voiceprint_samples")


def test_human_review_visibility_requires_role_and_assignment_scope() -> None:
    task = {"queue": "amount_conflict", "assignee_id": "u_read_policy"}
    foreign_task = {"queue": "amount_conflict", "assignee_id": "u_other"}
    blind_task = {"queue": "blind_calibration", "assignee_id": "u_other"}

    assert not can_read_human_review_task(task, _context("model_engineer"))
    assert can_read_human_review_task(task, _context("annotator"))
    assert not can_read_human_review_task(foreign_task, _context("annotator"))
    assert can_read_human_review_task(foreign_task, _context("review_arbitrator"))
    assert not can_read_human_review_task(blind_task, _context("project_admin"))


def test_aggregate_collection_allow_list_is_registered_and_fail_closed() -> None:
    model_collections = readable_resource_collections(_context("model_engineer"))

    assert "settings" in model_collections
    assert "human_review_tasks" not in model_collections
    assert "voiceprint_enrollments" not in model_collections
    assert not can_read_resource_collection(_context("system"), "unknown_projection")


def test_nested_trace_references_use_structured_sensitive_policy() -> None:
    model = _context("model_engineer")
    arbitrator = _context("review_arbitrator")
    visible_tasks = {"hrt-visible"}
    visible_decisions = {"hrd-visible"}

    assert not trace_reference_is_visible(
        {"type": "voiceprint_enrollment", "id": "vp-hidden"},
        model,
    )
    assert not trace_reference_is_visible(
        {"result_ref": {"voiceprint_id": "VP-HIDDEN"}},
        model,
    )
    assert not trace_reference_is_visible(
        {"type": "voiceprint_profile", "id": "VP-HIDDEN"},
        model,
    )
    assert not trace_reference_is_visible(
        {"voiceprint": {"id": "VP-HIDDEN"}},
        model,
    )
    assert not trace_reference_is_visible(
        {"result_ref": {"ref_type": "voiceprint-record", "ref_id": "VP-HIDDEN"}},
        model,
    )
    assert not trace_reference_is_visible(
        {"enrollment_id": "VP-HIDDEN"},
        model,
    )
    assert not trace_reference_is_visible(
        {"voiceprints": [{"id": "VP-HIDDEN"}]},
        model,
    )
    assert not trace_reference_is_visible(
        {"result_ref": {"review_decision_id": "HRD-HIDDEN"}},
        model,
    )
    assert not trace_reference_is_visible(
        {"result_ref": {"refType": "voiceprint_profile", "refId": "VP-HIDDEN"}},
        model,
    )
    assert not trace_reference_is_visible(
        {"type": "future_sensitive_resource", "id": "future-hidden"},
        model,
    )
    assert not trace_reference_is_visible(
        {"affected_objects": [{"type": "human_review_task", "id": "hrt-hidden"}]},
        arbitrator,
        visible_review_task_ids=visible_tasks,
        visible_review_decision_ids=visible_decisions,
    )
    assert trace_reference_is_visible(
        {
            "affected_objects": [
                {"type": "human_review_task", "id": "hrt-visible"},
                {"type": "human_review_decision", "id": "hrd-visible"},
            ]
        },
        arbitrator,
        visible_review_task_ids=visible_tasks,
        visible_review_decision_ids=visible_decisions,
    )
    assert trace_reference_is_visible(
        {"type": "metric_result", "id": "metric-visible"},
        model,
    )
    assert trace_reference_is_visible(
        {
            "affected_objects": [
                {
                    "type": "aggregation_policy_version",
                    "id": "label_policy_quote_amount_v3",
                }
            ]
        },
        model,
    )
    assert trace_reference_is_visible(
        {"result_ref": {"storage_object_id": "storage-visible"}},
        model,
    )


@pytest.mark.parametrize(
    "payload",
    [
        {
            "type": "voiceprint_profile",
            "id": "VP-SECRET",
            "Type": "data_asset",
            "Id": "asset-safe",
        },
        {
            "result_ref": {
                "refType": "voiceprint_profile",
                "refId": "VP-SECRET",
                "RefType": "data_asset",
                "RefId": "asset-safe",
            }
        },
        {
            "result_ref": {
                "refType": "data_asset",
                "refId": "asset-safe",
                "REFType": "voiceprint_profile",
                "REFId": "VP-SECRET",
            }
        },
        {
            "result_ref": {
                "resourceType": "data_asset",
                "resourceId": "asset-safe",
                "RESOURCEType": "voiceprint_profile",
                "RESOURCEId": "VP-SECRET",
            }
        },
        {
            "result_ref": {
                "aggregateType": "data_asset",
                "aggregateId": "asset-safe",
                "AGGREGATEType": "voiceprint_profile",
                "AGGREGATEId": "VP-SECRET",
            }
        },
        {
            "result_ref": {
                "objectType": "data_asset",
                "objectId": "asset-safe",
                "OBJECTType": "voiceprint_profile",
                "OBJECTId": "VP-SECRET",
            }
        },
        {
            "result_ref": {
                "subjectType": "data_asset",
                "subjectId": "asset-safe",
                "SUBJECTType": "voiceprint_profile",
                "SUBJECTId": "VP-SECRET",
            }
        },
        {
            "result_ref": {
                "REFTYPE": "voiceprint_profile",
                "REFID": "VP-SECRET",
            }
        },
        {
            "write_policy": {
                "enrollmentId": "VP-SECRET",
                "EnrollmentId": "asset-safe",
            }
        },
        {
            "adapter_dispatch": {
                "reviewDecisionId": "HRD-SECRET",
                "ReviewDecisionId": "asset-safe",
            }
        },
    ],
)
def test_trace_reference_normalization_collisions_fail_closed(payload: dict[str, object]) -> None:
    assert not trace_reference_is_visible(payload, _context("model_engineer"))


def test_trace_reference_ids_collects_exact_linked_review_rows() -> None:
    payload = {
        "review_task_id": "hrt-current",
        "source_review_task_id": "hrt-source",
        "source_decision_id": "hrd-source",
        "nested": [
            {"type": "human_review_decision", "id": "hrd-nested"},
            {"type": "voiceprint_enrollment", "id": "vp-hidden"},
        ],
        "description": "human_review_task=hrt-string-must-not-match",
    }

    assert trace_reference_ids(payload, "human_review_tasks") == {
        "hrt-current",
        "hrt-source",
    }
    assert trace_reference_ids(payload, "human_review_decisions") == {
        "hrd-source",
        "hrd-nested",
    }
    assert trace_reference_ids(payload, "voiceprint_enrollments") == {"vp-hidden"}


def test_unregistered_resource_fails_closed() -> None:
    with pytest.raises(ApiError) as exc_info:
        require_resource_read(_context("project_admin"), "future_sensitive_collection")

    assert exc_info.value.code == "RESOURCE_READ_POLICY_UNREGISTERED"
    assert exc_info.value.status_code == 500


def test_unregistered_resource_fails_closed_for_system_context() -> None:
    with pytest.raises(ApiError) as exc_info:
        require_resource_read(_context("system"), "future_system_collection")

    assert exc_info.value.code == "RESOURCE_READ_POLICY_UNREGISTERED"


def test_all_static_audit_and_outbox_object_types_have_trace_policies() -> None:
    app_root = Path(__file__).resolve().parents[2] / "app"
    unresolved: dict[str, set[str]] = {}
    for path in app_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg not in {"aggregate_type", "object_type"}:
                    continue
                if not isinstance(keyword.value, ast.Constant) or not isinstance(
                    keyword.value.value, str
                ):
                    continue
                object_type = keyword.value.value
                if trace_reference_collection(object_type) is None:
                    unresolved.setdefault(object_type, set()).add(
                        f"{path.relative_to(app_root)}:{node.lineno}"
                    )

    assert not unresolved, {
        object_type: sorted(locations) for object_type, locations in sorted(unresolved.items())
    }


@pytest.mark.parametrize(
    ("object_type", "collection"),
    [
        ("insight_report_metric_binding", "insight_reports"),
        ("label_fact_backfill", "label_aggregates"),
        ("label_fact_set", "label_aggregates"),
        ("label_fact_set_head", "label_aggregates"),
        ("label_fact_set_head_event", "label_aggregates"),
        ("label_mapping_bundle", "label_versions"),
        ("label_mapping_version", "label_versions"),
        ("label_recompute_run", "label_recompute_runs"),
        ("label_recompute_run_item", "label_recompute_runs"),
        ("label_version_deprecation_preflight", "label_versions"),
        ("oidc_identity", "oidc_identities"),
        ("browser_auth_session", "browser_auth_sessions"),
        ("qdrant_rebuild_plan", "qdrant_rebuild_plans"),
        ("release_bundle_head_event", "task_versions"),
        ("task_run_cancellation", "task_versions"),
        ("task_run_status_sync", "task_versions"),
    ],
)
def test_new_governed_trace_types_resolve_to_their_parent_collection(
    object_type: str,
    collection: str,
) -> None:
    assert trace_reference_collection(object_type) == collection


def test_all_static_typed_id_references_have_trace_policies() -> None:
    app_root = Path(__file__).resolve().parents[2] / "app"
    unresolved: dict[str, set[str]] = {}
    for path in app_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            fields = {
                key.value: value
                for key, value in zip(node.keys, node.values, strict=True)
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
            object_type = fields.get("type")
            if "id" not in fields or not isinstance(object_type, ast.Constant):
                continue
            if not isinstance(object_type.value, str):
                continue
            if trace_reference_collection(object_type.value) is None:
                unresolved.setdefault(object_type.value, set()).add(
                    f"{path.relative_to(app_root)}:{node.lineno}"
                )

    assert not unresolved, {
        object_type: sorted(locations) for object_type, locations in sorted(unresolved.items())
    }


def test_policy_registry_is_immutable() -> None:
    with pytest.raises(TypeError):
        RESOURCE_READ_POLICIES["runtime_override"] = RESOURCE_READ_POLICIES["data_assets"]  # type: ignore[index]


def test_assignment_scope_compiles_for_mysql_json_fields() -> None:
    scope = resource_read_scope(_context("annotator"), "human_review_tasks")

    assert scope is not None
    compiled = str(select(JsonResource.id).where(scope).compile(dialect=mysql.dialect()))
    assert "json_resources.data" in compiled
    assert "blind_calibration" not in compiled  # Values remain bound parameters.


def test_policy_registry_covers_runtime_projection_collections() -> None:
    expected = {
        "asr_segments",
        "audio_quality_reports",
        "audio_sessions",
        "connectors",
        "conversation_boundaries",
        "data_aggregation_views",
        "data_assets",
        "data_source_records",
        "documents",
        "eval_datasets",
        "event_links",
        "evidence_packs",
        "human_review_tasks",
        "human_review_decisions",
        "insight_reports",
        "insight_funnels",
        "knowledge_effects",
        "knowledge_indexes",
        "knowledge_quality_gates",
        "knowledge_sources",
        "label_aggregates",
        "label_candidates",
        "label_taxonomy_suggestions",
        "label_versions",
        "listening_annotations",
        "platform_sessions",
        "prompt_version_candidates",
        "recordings",
        "settings",
        "settings_drafts",
        "speaker_turns",
        "task_types",
        "task_versions",
        "taxonomies",
        "vad_segments",
        "voiceprint_enrollments",
        "voiceprint_samples",
        "work_items",
    }

    assert set(RESOURCE_READ_POLICIES) == expected
