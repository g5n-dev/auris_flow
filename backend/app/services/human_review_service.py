from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.core.request_identifiers import server_generated_public_id
from app.models import (
    EvidencePack,
    HumanReviewDecision,
    HumanReviewTask,
    InsightAction,
    JsonResource,
    LabelAggregate,
    LabelAggregationPolicyVersion,
    LabelConflict,
    LabelTaxonomySuggestion,
    PromptVersionCandidate,
    RunRecord,
)
from app.services.audio_evidence_review_service import get_scoped_evidence_pack
from app.services.audit_service import record_audit
from app.services.human_review_readback_service import (
    enrich_human_review_affected_objects,
)
from app.services.label_closed_loop_service import materialize_human_review_feedback
from app.services.label_review_projection_service import sync_label_review_projection
from app.services.outbox_service import enqueue_event
from app.services.resource_service import list_resources, upsert_resource
from app.services.review_callback_service import create_review_platform_callbacks

Decision = str

DECISION_ALIASES: dict[str, Decision] = {
    "accepted": "accepted",
    "approved": "accepted",
    "confirm": "accepted",
    "modified": "modified",
    "rejected": "rejected",
    "blocked": "rejected",
    "escalate": "escalated",
    "escalated": "escalated",
}

TASK_STATUS_BY_DECISION = {
    "accepted": "success",
    "modified": "success",
    "rejected": "blocked",
    "escalated": "escalated",
}

REVIEW_OPEN_STATES = frozenset({"pending", "escalated"})
TERMINAL_DECISIONS = frozenset({"accepted", "modified", "rejected"})

LABEL_CONFLICT_STATUS_BY_DECISION = {
    "accepted": "resolved",
    "modified": "resolved",
    "rejected": "resolved",
    "escalated": "reviewing",
}
LABEL_CONFLICT_OPEN_STATES = frozenset({"detected", "reviewing"})

TARGET_STATUS_BY_DECISION = {
    "accepted": "success",
    "modified": "success",
    "rejected": "blocked",
    "escalated": "pending",
}

EVENT_RELATION_STATE_BY_DECISION = {
    "accepted": "confirmed",
    "modified": "modified",
    "rejected": "rejected",
    "escalated": "needs_human",
}

VOICEPRINT_STATE_BY_DECISION = {
    "accepted": "confirmed",
    "modified": "confirmed",
    "rejected": "rejected",
    "escalated": "pending_review",
}

TARGET_COLLECTION_BY_TYPE = {
    "label_candidate": "label_candidates",
    "label_candidates": "label_candidates",
    "label_aggregate": "label_aggregates",
    "label_aggregates": "label_aggregates",
    "prompt_version_candidate": "prompt_version_candidates",
    "prompt_version_candidates": "prompt_version_candidates",
    "taxonomy_suggestion": "label_taxonomy_suggestions",
    "label_taxonomy_suggestion": "label_taxonomy_suggestions",
    "event_link": "event_links",
    "event_links": "event_links",
    "evidence_pack": "evidence_packs",
    "evidence_packs": "evidence_packs",
    "conversation_boundary": "conversation_boundaries",
    "conversation_boundaries": "conversation_boundaries",
    "voiceprint_sample": "voiceprint_samples",
    "voiceprint_samples": "voiceprint_samples",
    "work_item": "work_items",
    "work_items": "work_items",
}

PUBLIC_TARGET_TYPE_BY_COLLECTION = {
    "label_candidates": "label_candidate",
    "label_aggregates": "label_aggregate",
    "prompt_version_candidates": "prompt_version_candidate",
    "label_taxonomy_suggestions": "taxonomy_suggestion",
    "event_links": "event_link",
    "evidence_packs": "evidence_pack",
    "conversation_boundaries": "conversation_boundary",
    "voiceprint_samples": "voiceprint_sample",
    "work_items": "work_item",
}

CLOSED_LOOP_TARGET_COLLECTIONS = frozenset(
    {"label_aggregates", "prompt_version_candidates", "label_taxonomy_suggestions"}
)


def _rooted_context(ctx: RequestContext, root_trace_id: str) -> RequestContext:
    if ctx.trace_id == root_trace_id:
        return ctx
    return replace(
        ctx,
        trace_id=root_trace_id,
        parent_trace_id=ctx.trace_id,
        correlation_id=root_trace_id,
    )


def normalize_review_decision(value: str | None) -> Decision:
    decision = DECISION_ALIASES.get(str(value or "accepted"))
    if not decision:
        raise ApiError(
            "INVALID_REVIEW_DECISION",
            "人审决策必须是 accepted、modified、rejected 或 escalated",
            422,
        )
    return decision


def get_human_review_task_for_update(
    session: Session,
    ctx: RequestContext,
    review_task_id: str,
) -> tuple[JsonResource, HumanReviewTask]:
    projection = session.scalar(
        select(HumanReviewTask)
        .where(
            HumanReviewTask.review_task_id == review_task_id,
            HumanReviewTask.tenant_id == ctx.tenant_id,
            HumanReviewTask.project_id == ctx.project_id,
        )
        .with_for_update()
    )
    if projection is None:
        raise ApiError("NOT_FOUND", f"human_review_tasks 不存在：{review_task_id}", 404)

    task = session.scalar(
        select(JsonResource)
        .where(
            JsonResource.collection == "human_review_tasks",
            JsonResource.resource_key == review_task_id,
            JsonResource.tenant_id == ctx.tenant_id,
            JsonResource.project_id == ctx.project_id,
        )
        .with_for_update()
    )
    if task is None:
        raise ApiError(
            "HUMAN_REVIEW_TASK_PROJECTION_MISSING",
            "人审任务强表与业务投影不一致",
            409,
        )
    return task, projection


def apply_human_review_decision(
    session: Session,
    ctx: RequestContext,
    *,
    task: JsonResource,
    task_projection: HumanReviewTask,
    request_body: dict[str, Any],
) -> dict[str, Any]:
    task_before = dict(task.data)
    action_trace_id = str(request_body.get("_action_trace_id") or ctx.trace_id)
    strong_evidence = _validate_audio_evidence_task_binding(
        session,
        ctx,
        task_data=task_before,
        task_projection=task_projection,
        review_task_id=str(task.resource_key),
    )
    source_trace_id = str(
        task_before.get("source_trace_id") or task_before.get("trace_id") or ctx.trace_id
    )
    explicit_root_trace_id = task_before.get("root_trace_id")
    root_trace_id = (
        strong_evidence.root_trace_id
        if strong_evidence is not None
        else str(explicit_root_trace_id)
        if isinstance(explicit_root_trace_id, str) and explicit_root_trace_id
        else source_trace_id
        if _closed_loop_refs(task_before.get("target_refs") or [])
        else ctx.trace_id
    )
    # Every review decision remains on the business root, regardless of whether
    # it is a label-governance task or an audio-evidence task. The HTTP action
    # trace is retained separately for request-level observability.
    ctx = _rooted_context(ctx, root_trace_id)
    decision = normalize_review_decision(request_body.get("decision"))
    _validate_closed_loop_task_binding(
        session,
        ctx,
        task_data=task_before,
        task_projection=task_projection,
        review_task_id=str(task.resource_key),
    )
    _validate_decision_is_allowed(
        task_before,
        decision,
        request_body,
        projection_status=task_projection.status,
    )

    now = datetime.now(UTC).isoformat()
    review_task_id = str(task.resource_key)
    evidence_pack_id = str(task_before.get("evidence_pack_id") or "")
    decision_id = server_generated_public_id("hrd", suffix_length=12)
    affected_objects: list[dict[str, str]] = []
    label_conflicts = _resolve_label_conflicts_for_update(
        session,
        ctx,
        task_before=task_before,
    )
    label_conflict_befores = {
        conflict.conflict_id: _label_conflict_snapshot(conflict) for conflict in label_conflicts
    }

    task.data = {
        **task.data,
        "status": TASK_STATUS_BY_DECISION[decision],
        "review_status": decision,
        "decision": decision,
        "decision_id": decision_id,
        "decision_note": request_body.get("note") or request_body.get("reason"),
        "decided_by": ctx.user_id,
        "decided_at": now,
        "trace_id": ctx.trace_id,
        "action_trace_id": action_trace_id,
        "decision_history": [
            *(task.data.get("decision_history") or []),
            {
                "decision_id": decision_id,
                "decision": decision,
                "note": request_body.get("note") or request_body.get("reason"),
                "decided_by": ctx.user_id,
                "decided_at": now,
                "trace_id": ctx.trace_id,
                "action_trace_id": action_trace_id,
            },
        ],
    }
    task.status = task.data["status"]
    task.trace_id = ctx.trace_id
    sync_label_review_projection(
        session,
        ctx,
        "human_review_tasks",
        review_task_id,
        task.data,
        status=task.status,
        trace_id=task.trace_id,
    )
    affected_objects.append({"type": "human_review_task", "id": review_task_id})

    target_resources = _resolve_review_targets(session, ctx, task_before=task_before)
    change_map = _review_change_map(request_body)
    resolved_target_keys = {_target_key(target) for target in target_resources}
    unknown_change_targets = sorted(set(change_map) - resolved_target_keys)
    if unknown_change_targets:
        raise ApiError(
            "REVIEW_TARGET_NOT_BOUND_TO_TASK",
            "修改目标不属于当前人审任务证据链",
            422,
            details=[{"targets": unknown_change_targets}],
        )
    target_befores: dict[str, dict[str, Any]] = {}
    for target in target_resources:
        target_befores[_target_key(target)] = dict(target.data)
        _validate_closed_loop_target_changes(
            session,
            ctx,
            target=target,
            fields=change_map.get(_target_key(target), {}),
        )
        _apply_target_writeback(
            target,
            decision=decision,
            decision_id=decision_id,
            review_task_id=review_task_id,
            note=request_body.get("note") or request_body.get("reason"),
            field_changes=change_map.get(_target_key(target), {}),
            ctx=ctx,
        )
        _sync_insight_action_review(session, ctx, target, decision)
        sync_label_review_projection(
            session,
            ctx,
            target.collection,
            str(target.resource_key),
            target.data,
            status=target.status,
            trace_id=target.trace_id,
        )
        affected_objects.append(
            {"type": _public_target_type(target.collection), "id": str(target.resource_key)}
        )
        record_audit(
            session,
            ctx,
            action="human_review.target_writeback",
            object_type=target.collection,
            object_id=str(target.resource_key),
            before=target_befores[_target_key(target)],
            after=target.data,
        )

    for conflict in label_conflicts:
        _apply_label_conflict_writeback(
            conflict,
            decision=decision,
            decision_id=decision_id,
            review_task_id=review_task_id,
            note=request_body.get("note") or request_body.get("reason"),
            decided_at=now,
            ctx=ctx,
        )
        affected_objects.append({"type": "label_conflict", "id": conflict.conflict_id})
        record_audit(
            session,
            ctx,
            action="human_review.label_conflict_writeback",
            object_type="label_conflict",
            object_id=conflict.conflict_id,
            before=label_conflict_befores[conflict.conflict_id],
            after=_label_conflict_snapshot(conflict),
        )

    # Persist and flush the immutable human authority source before any
    # downstream LabelFact references it. The surrounding transaction remains
    # atomic; after feedback materialization we replace this preliminary payload
    # with the complete affected-object manifest.
    decision_payload = {
        "decision_id": decision_id,
        "review_task_id": review_task_id,
        "decision": decision,
        "note": request_body.get("note") or request_body.get("reason"),
        "decided_by": ctx.user_id,
        "decided_at": now,
        "source_trace_id": source_trace_id,
        "trace_id": ctx.trace_id,
        "action_trace_id": action_trace_id,
        "evidence_pack_id": evidence_pack_id or None,
        "affected_objects": affected_objects,
        "before_json": {
            "human_review_task": task_before,
            "targets": target_befores,
            "label_conflicts": label_conflict_befores,
        },
        "after_json": {
            "human_review_task": task.data,
            "targets": {_target_key(target): dict(target.data) for target in target_resources},
            "label_conflicts": {
                conflict.conflict_id: _label_conflict_snapshot(conflict)
                for conflict in label_conflicts
            },
        },
    }
    decision_record = HumanReviewDecision(
        decision_id=decision_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        review_task_id=review_task_id,
        terminal_review_task_id=(review_task_id if decision in TERMINAL_DECISIONS else None),
        status=TASK_STATUS_BY_DECISION[decision],
        trace_id=ctx.trace_id,
        payload=decision_payload,
    )
    session.add(decision_record)
    # Flush the terminal-decision uniqueness constraint before creating secondary
    # projections, runs and outbox events. The surrounding transaction rolls back
    # every target write if another worker already committed a terminal decision.
    session.flush()

    affected_objects.extend(
        materialize_human_review_feedback(
            session,
            ctx,
            decision_id=decision_id,
            review_task_id=review_task_id,
            decision=decision,
            note=request_body.get("note") or request_body.get("reason"),
            target_resources=target_resources,
            target_befores=target_befores,
        )
    )
    affected_objects.append({"type": "human_review_decision", "id": decision_id})
    callback_binding_payload = task_before
    if strong_evidence is not None and "output_sink_refs" in strong_evidence.payload:
        callback_binding_payload = {
            **task_before,
            "output_sink_refs": strong_evidence.payload["output_sink_refs"],
        }
    if decision in TERMINAL_DECISIONS:
        affected_objects.extend(
            create_review_platform_callbacks(
                session,
                ctx,
                task_payload=callback_binding_payload,
                decision_payload={
                    **decision_payload,
                    "affected_objects": affected_objects,
                },
            )
        )
    enriched_affected_objects = enrich_human_review_affected_objects(
        session,
        ctx,
        decision_id=decision_id,
        affected_objects=affected_objects,
    )
    decision_payload = {
        **decision_payload,
        "affected_objects": enriched_affected_objects,
    }
    decision_record.payload = decision_payload
    upsert_resource(
        session,
        ctx,
        "human_review_decisions",
        decision_id,
        decision_payload,
        status=TASK_STATUS_BY_DECISION[decision],
        trace_id=ctx.trace_id,
        audit_action="human_review.decision.created",
    )
    session.add(
        RunRecord(
            run_id=decision_id,
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            run_type="human_review_decision",
            status=TASK_STATUS_BY_DECISION[decision],
            run_key=f"human-review:{review_task_id}:{decision}",
            partition_key=f"{ctx.tenant_id}/{ctx.project_id}/{evidence_pack_id or review_task_id}",
            trace_id=ctx.trace_id,
            payload=decision_payload,
        )
    )
    enqueue_event(
        session,
        ctx,
        event_type="human_review.decision.created",
        aggregate_type="human_review_decision",
        aggregate_id=decision_id,
        payload=decision_payload,
    )
    record_audit(
        session,
        ctx,
        action="human_review_task.status_changed",
        object_type="human_review_task",
        object_id=review_task_id,
        before=task_before,
        after=task.data,
    )
    queue = str(task_before.get("queue") or "").strip()
    next_review_route = "/api/v1/human-review-tasks?status=pending"
    if queue:
        next_review_route += f"&queue={quote(queue, safe='')}"
    return {
        "resource_type": "human_review_decision",
        "resource_id": decision_id,
        "decision_id": decision_id,
        "decision": decision,
        "status": task.data["status"],
        "root_trace_id": root_trace_id,
        "current_trace_id": action_trace_id,
        "trace_id": root_trace_id,
        "action_trace_id": action_trace_id,
        "readback_url": f"/api/v1/human-review-tasks/{review_task_id}",
        "readback_urls": [
            f"/api/v1/human-review-tasks/{review_task_id}",
            *([f"/api/v1/evidence-packs/{evidence_pack_id}"] if evidence_pack_id else []),
        ],
        "affected_objects": enriched_affected_objects,
        "next_actions": [
            {
                "key": "next_review",
                "label": "确认下一通",
                "route": next_review_route,
            },
            {
                "key": "view_trace",
                "label": "查看 Trace",
                "route": f"traces/{root_trace_id}",
            },
        ],
    }


def _validate_decision_is_allowed(
    task_data: dict[str, Any],
    decision: Decision,
    request_body: dict[str, Any],
    *,
    projection_status: str,
) -> None:
    current_decision = task_data.get("decision")
    current_review_state = str(task_data.get("review_status") or task_data.get("status") or "")
    projection_state = str(projection_status or "")
    has_terminal_decision = bool(
        current_decision and normalize_review_decision(str(current_decision)) in TERMINAL_DECISIONS
    )
    if (
        has_terminal_decision
        or current_review_state not in REVIEW_OPEN_STATES
        or projection_state not in REVIEW_OPEN_STATES
    ):
        raise ApiError(
            "HUMAN_REVIEW_TASK_ALREADY_DECIDED",
            "该人审任务已进入终态，不能重复落账或覆盖决策",
            409,
            details=[
                {
                    "review_status": current_review_state,
                    "projection_status": projection_state,
                    "decision": current_decision,
                    "decision_id": task_data.get("decision_id"),
                }
            ],
        )
    if decision != "modified":
        if request_body.get("changes"):
            raise ApiError(
                "REVIEW_DECISION_CHANGES_NOT_ALLOWED",
                "只有修改后接受可以提交字段变更",
                422,
            )
        return
    if not request_body.get("changes"):
        raise ApiError(
            "REVIEW_DECISION_CHANGES_REQUIRED",
            "修改后接受必须提供受控 changes",
            422,
        )


def _resolve_review_targets(
    session: Session,
    ctx: RequestContext,
    *,
    task_before: dict[str, Any],
) -> list[JsonResource]:
    resources: dict[str, JsonResource] = {}
    evidence_pack_id = task_before.get("evidence_pack_id")

    closed_loop_refs = _closed_loop_refs(task_before.get("target_refs") or [])

    task_targets = (
        list(task_before.get("target_refs") or [])
        if closed_loop_refs
        else [
            *(task_before.get("target_refs") or []),
            *(task_before.get("affected_objects") or []),
        ]
    )
    for target in task_targets:
        if not isinstance(target, dict):
            continue
        collection = TARGET_COLLECTION_BY_TYPE.get(str(target.get("type") or ""))
        target_id = target.get("id")
        if not collection or not isinstance(target_id, str) or not target_id:
            continue
        resource = session.scalar(
            select(JsonResource)
            .where(
                JsonResource.collection == collection,
                JsonResource.resource_key == target_id,
                JsonResource.tenant_id == ctx.tenant_id,
                JsonResource.project_id == ctx.project_id,
            )
            .with_for_update()
        )
        if resource is None and collection in CLOSED_LOOP_TARGET_COLLECTIONS:
            raise ApiError(
                "CLOSED_LOOP_TARGET_PROJECTION_MISSING",
                "闭环人审目标缺少当前租户项目内的业务投影",
                409,
                details=[{"collection": collection, "target_id": target_id}],
            )
        if resource is not None:
            resources[_target_key(resource)] = resource

    if not closed_loop_refs and isinstance(evidence_pack_id, str) and evidence_pack_id:
        _add_by_key(session, ctx, resources, "evidence_packs", evidence_pack_id)
        for resource in list_resources(session, ctx, "label_candidates", limit=200):
            if resource.data.get("evidence_pack_id") == evidence_pack_id:
                resources[_target_key(resource)] = resource
        for resource in list_resources(session, ctx, "event_links", limit=200):
            if _event_link_mentions_evidence_pack(resource.data, evidence_pack_id):
                resources[_target_key(resource)] = resource
    return list(resources.values())


def _closed_loop_refs(raw_refs: object) -> list[tuple[str, str]]:
    refs: list[tuple[str, str]] = []
    if not isinstance(raw_refs, list):
        return refs
    for target in raw_refs:
        if not isinstance(target, dict):
            continue
        collection = TARGET_COLLECTION_BY_TYPE.get(str(target.get("type") or ""))
        target_id = target.get("id")
        if (
            collection in CLOSED_LOOP_TARGET_COLLECTIONS
            and isinstance(target_id, str)
            and target_id
        ):
            refs.append((collection, target_id))
    return refs


def _validate_closed_loop_task_binding(
    session: Session,
    ctx: RequestContext,
    *,
    task_data: dict[str, Any],
    task_projection: HumanReviewTask,
    review_task_id: str,
) -> None:
    target_refs = task_data.get("target_refs") or []
    closed_loop_refs = _closed_loop_refs(target_refs)
    affected_closed_loop_refs = _closed_loop_refs(task_data.get("affected_objects") or [])
    if not closed_loop_refs and not affected_closed_loop_refs:
        return
    if (
        len(closed_loop_refs) != 1
        or len(target_refs) != 1
        or affected_closed_loop_refs
        or task_projection.payload.get("target_refs") != target_refs
    ):
        raise ApiError(
            "CLOSED_LOOP_REVIEW_TASK_BINDING_INVALID",
            "闭环人审任务必须只通过一个显式 target_refs 绑定一个强类型对象",
            409,
        )
    collection, target_id = closed_loop_refs[0]
    if collection == "label_aggregates":
        target = session.scalar(
            select(LabelAggregate).where(
                LabelAggregate.aggregate_id == target_id,
                LabelAggregate.tenant_id == ctx.tenant_id,
                LabelAggregate.project_id == ctx.project_id,
            )
        )
        bound_task_id = target.review_task_id if target is not None else None
    elif collection == "label_taxonomy_suggestions":
        suggestion = session.scalar(
            select(LabelTaxonomySuggestion).where(
                LabelTaxonomySuggestion.suggestion_id == target_id,
                LabelTaxonomySuggestion.tenant_id == ctx.tenant_id,
                LabelTaxonomySuggestion.project_id == ctx.project_id,
            )
        )
        bound_task_id = suggestion.review_task_id if suggestion is not None else None
    else:
        candidate = session.scalar(
            select(PromptVersionCandidate).where(
                PromptVersionCandidate.candidate_id == target_id,
                PromptVersionCandidate.tenant_id == ctx.tenant_id,
                PromptVersionCandidate.project_id == ctx.project_id,
            )
        )
        bound_task_id = candidate.payload.get("review_task_id") if candidate is not None else None
    if bound_task_id != review_task_id:
        raise ApiError(
            "CLOSED_LOOP_REVIEW_TASK_STRONG_BINDING_MISMATCH",
            "闭环目标强表 review_task_id 与当前任务不一致",
            409,
            details=[
                {
                    "target_id": target_id,
                    "expected_review_task_id": bound_task_id,
                    "actual_review_task_id": review_task_id,
                }
            ],
        )


def _validate_audio_evidence_task_binding(
    session: Session,
    ctx: RequestContext,
    *,
    task_data: dict[str, Any],
    task_projection: HumanReviewTask,
    review_task_id: str,
) -> EvidencePack | None:
    raw_evidence_pack_id = task_data.get("evidence_pack_id")
    if raw_evidence_pack_id is None:
        return None
    if not isinstance(raw_evidence_pack_id, str) or not raw_evidence_pack_id.strip():
        raise ApiError(
            "AUDIO_EVIDENCE_TASK_BINDING_INVALID",
            "音频人审任务的 EvidencePack 引用无效",
            409,
        )
    evidence_pack_id = raw_evidence_pack_id.strip()
    evidence = get_scoped_evidence_pack(
        session,
        ctx,
        evidence_pack_id,
        for_update=True,
    )
    projection_payload = (
        task_projection.payload if isinstance(task_projection.payload, dict) else {}
    )
    raw_target_refs = task_data.get("target_refs")
    target_refs = raw_target_refs if isinstance(raw_target_refs, list) else []
    has_evidence_target = any(
        isinstance(target, dict) and target.get("type") in {"evidence_pack", "evidence_packs"}
        for target in target_refs
    )
    is_audio_evidence_review = (
        task_data.get("queue") == "audio_evidence_review" or has_evidence_target
    )
    if projection_payload.get("evidence_pack_id") != evidence_pack_id or projection_payload.get(
        "target_refs"
    ) != task_data.get("target_refs"):
        raise ApiError(
            "AUDIO_EVIDENCE_TASK_BINDING_MISMATCH",
            "HumanReviewTask 强表与业务投影的证据绑定不一致",
            409,
            details=[{"review_task_id": review_task_id, "evidence_pack_id": evidence_pack_id}],
        )
    expected_values = (
        {
            "audio_session_id": evidence.audio_session_id,
            "recording_id": evidence.recording_id,
            "root_trace_id": evidence.root_trace_id,
            "source_run_id": evidence.source_run_id,
        }
        if is_audio_evidence_review
        else {}
    )
    mismatches = [
        {
            "field": field,
            "expected": expected,
            "actual": actual,
        }
        for field, expected in expected_values.items()
        if (actual := task_data.get(field)) is not None and actual != expected
    ]
    if mismatches:
        raise ApiError(
            "AUDIO_EVIDENCE_TASK_BINDING_MISMATCH",
            "人审任务与不可变 EvidencePack 的业务绑定不一致",
            409,
            details=mismatches,
        )

    evidence_refs = [
        target
        for target in target_refs
        if isinstance(target, dict) and target.get("type") in {"evidence_pack", "evidence_packs"}
    ]
    legacy_seed = (
        evidence.status == "superseded" and evidence.source_run_id == "seed:legacy-evidence-import"
    )
    if is_audio_evidence_review and not legacy_seed and evidence.status != "ready":
        raise ApiError(
            "AUDIO_EVIDENCE_NOT_REVIEWABLE",
            "只有 ready EvidencePack 可以进入生产人审",
            409,
            details=[{"evidence_pack_id": evidence_pack_id, "status": evidence.status}],
        )
    if (
        is_audio_evidence_review
        and not legacy_seed
        and (len(evidence_refs) != 1 or evidence_refs[0].get("id") != evidence_pack_id)
    ):
        raise ApiError(
            "AUDIO_EVIDENCE_TASK_BINDING_INVALID",
            "音频人审任务必须显式且唯一绑定不可变 EvidencePack",
            409,
        )
    for target in target_refs if is_audio_evidence_review else []:
        if not isinstance(target, dict):
            raise ApiError(
                "AUDIO_EVIDENCE_TASK_TARGET_INVALID",
                "音频人审任务包含无效目标引用",
                409,
            )
        collection = TARGET_COLLECTION_BY_TYPE.get(str(target.get("type") or ""))
        target_id = target.get("id")
        if (
            collection
            not in {
                "evidence_packs",
                "conversation_boundaries",
                "event_links",
                "label_candidates",
            }
            or not isinstance(target_id, str)
            or not target_id
        ):
            raise ApiError(
                "AUDIO_EVIDENCE_TASK_TARGET_INVALID",
                "音频人审任务只能绑定已物化的受控证据目标",
                409,
            )
        target_projection = session.scalar(
            select(JsonResource).where(
                JsonResource.collection == collection,
                JsonResource.resource_key == target_id,
                JsonResource.tenant_id == ctx.tenant_id,
                JsonResource.project_id == ctx.project_id,
            )
        )
        if target_projection is None:
            raise ApiError(
                "AUDIO_EVIDENCE_TASK_TARGET_MISSING",
                "音频人审任务引用的受控目标尚未物化",
                409,
                details=[{"collection": collection, "target_id": target_id}],
            )
    return evidence if is_audio_evidence_review else None


def _resolve_label_conflicts_for_update(
    session: Session,
    ctx: RequestContext,
    *,
    task_before: dict[str, Any],
) -> list[LabelConflict]:
    conflicts: list[LabelConflict] = []
    for conflict_id in _label_conflict_ids(task_before):
        conflict = session.scalar(
            select(LabelConflict)
            .where(
                LabelConflict.conflict_id == conflict_id,
                LabelConflict.tenant_id == ctx.tenant_id,
                LabelConflict.project_id == ctx.project_id,
            )
            .with_for_update()
        )
        if conflict is None:
            raise ApiError(
                "HUMAN_REVIEW_LABEL_CONFLICT_NOT_FOUND",
                "人审任务引用的标签冲突在当前租户项目中不存在",
                409,
                details=[{"label_conflict_id": conflict_id}],
            )
        if conflict.status not in LABEL_CONFLICT_OPEN_STATES:
            raise ApiError(
                "HUMAN_REVIEW_LABEL_CONFLICT_ALREADY_RESOLVED",
                "人审任务引用的标签冲突已关闭，不能重复覆盖",
                409,
                details=[
                    {
                        "label_conflict_id": conflict_id,
                        "status": conflict.status,
                        "review_decision_id": conflict.payload.get("review_decision_id"),
                    }
                ],
            )
        conflicts.append(conflict)
    return conflicts


def _label_conflict_ids(task_before: dict[str, Any]) -> list[str]:
    conflict_ids: set[str] = set()
    direct_conflict_id = task_before.get("label_conflict_id")
    if direct_conflict_id is not None:
        if not isinstance(direct_conflict_id, str) or not direct_conflict_id:
            raise ApiError(
                "HUMAN_REVIEW_LABEL_CONFLICT_REFERENCE_INVALID",
                "人审任务的标签冲突引用无效",
                409,
            )
        conflict_ids.add(direct_conflict_id)

    for target in task_before.get("target_refs") or []:
        if not isinstance(target, dict):
            continue
        if str(target.get("type") or "") not in {"label_conflict", "label_conflicts"}:
            continue
        conflict_id = target.get("id")
        if not isinstance(conflict_id, str) or not conflict_id:
            raise ApiError(
                "HUMAN_REVIEW_LABEL_CONFLICT_REFERENCE_INVALID",
                "人审任务的标签冲突引用无效",
                409,
            )
        conflict_ids.add(conflict_id)
    return sorted(conflict_ids)


def _label_conflict_snapshot(conflict: LabelConflict) -> dict[str, Any]:
    return {
        "conflict_id": conflict.conflict_id,
        "tenant_id": conflict.tenant_id,
        "project_id": conflict.project_id,
        "status": conflict.status,
        "trace_id": conflict.trace_id,
        "payload": dict(conflict.payload),
    }


def _apply_label_conflict_writeback(
    conflict: LabelConflict,
    *,
    decision: Decision,
    decision_id: str,
    review_task_id: str,
    note: str | None,
    decided_at: str,
    ctx: RequestContext,
) -> None:
    status = LABEL_CONFLICT_STATUS_BY_DECISION[decision]
    source_trace_id = (
        conflict.payload.get("source_trace_id")
        or conflict.payload.get("trace_id")
        or conflict.trace_id
    )
    conflict.status = status
    conflict.trace_id = ctx.trace_id
    conflict.payload = {
        **conflict.payload,
        "status": status,
        "resolution": decision,
        "resolution_note": note,
        "review_decision_id": decision_id,
        "review_task_id": review_task_id,
        "decided_by": ctx.user_id,
        "decided_at": decided_at,
        "resolved_at": decided_at if decision in TERMINAL_DECISIONS else None,
        "source_trace_id": source_trace_id,
        "trace_id": ctx.trace_id,
    }


def _review_change_map(request_body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    changes: dict[str, dict[str, Any]] = {}
    for change in request_body.get("changes") or []:
        if not isinstance(change, dict):
            continue
        collection = TARGET_COLLECTION_BY_TYPE.get(str(change.get("target_type") or ""))
        target_id = change.get("target_id")
        fields = change.get("fields")
        if not collection or not isinstance(target_id, str) or not isinstance(fields, dict):
            continue
        key = f"{collection}:{target_id}"
        if key in changes:
            raise ApiError(
                "REVIEW_TARGET_CHANGE_DUPLICATE",
                "同一人审目标不能提交多组字段变更",
                422,
                details=[{"target": key}],
            )
        changes[key] = dict(fields)
    return changes


def _validate_label_value(kind: str, value: Any) -> None:
    valid = False
    if kind == "boolean":
        valid = isinstance(value, bool)
    elif kind in {"categorical", "hierarchical"}:
        valid = isinstance(value, str) and bool(value.strip())
    elif kind == "multi":
        valid = (
            isinstance(value, list)
            and bool(value)
            and all(isinstance(item, str) and bool(item.strip()) for item in value)
            and len(value) == len(set(value))
        )
    elif kind == "numeric":
        valid = (
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(float(value))
        )
    elif kind == "temporal" and isinstance(value, dict):
        start = value.get("start", value.get("start_ms"))
        end = value.get("end", value.get("end_ms"))
        valid = (
            not isinstance(start, bool)
            and isinstance(start, (int, float))
            and not isinstance(end, bool)
            and isinstance(end, (int, float))
            and math.isfinite(float(start))
            and math.isfinite(float(end))
            and float(start) <= float(end)
        )
    if not valid:
        raise ApiError(
            "LABEL_REVIEW_VALUE_TYPE_INVALID",
            "人工修改值不符合锁定标签类型",
            422,
            details=[{"kind": kind}],
        )


def _reject_unexpected_review_fields(
    fields: dict[str, Any],
    *,
    allowed_fields: set[str],
    code: str,
    message: str,
) -> None:
    unexpected = sorted(set(fields) - allowed_fields)
    if unexpected:
        raise ApiError(
            code,
            message,
            422,
            details=[
                {
                    "allowed_fields": sorted(allowed_fields),
                    "actual_fields": sorted(fields),
                }
            ],
        )


def _validate_review_text(value: object, *, field: str, maximum: int = 512) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.strip()) > maximum
        or any(ord(character) < 0x20 for character in value)
    ):
        raise ApiError(
            "HUMAN_REVIEW_FIELD_VALUE_INVALID",
            "人工修改字段包含空值、控制字符或超长文本",
            422,
            details=[{"field": field, "max_length": maximum}],
        )


def _validate_review_confidence(value: object, *, field: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 <= float(value) <= 1
    ):
        raise ApiError(
            "HUMAN_REVIEW_CONFIDENCE_INVALID",
            "人工修改置信度必须是 0 到 1 之间的有限数值",
            422,
            details=[{"field": field}],
        )


def _validate_review_identifier_list(value: object, *, field: str) -> None:
    if (
        not isinstance(value, list)
        or len(value) > 100
        or any(
            not isinstance(item, str) or not item.strip() or len(item.strip()) > 128
            for item in value
        )
        or len(value) != len(set(value))
    ):
        raise ApiError(
            "CONVERSATION_BOUNDARY_REVIEW_LIST_INVALID",
            "边界切片引用必须是去重且有界的标识符列表",
            422,
            details=[{"field": field, "max_items": 100}],
        )


def _validate_closed_loop_target_changes(
    session: Session,
    ctx: RequestContext,
    *,
    target: JsonResource,
    fields: dict[str, Any],
) -> None:
    if not fields:
        return
    if target.collection == "evidence_packs":
        allowed_fields = {"recording_disposition", "low_confidence"}
        if set(fields) - allowed_fields:
            raise ApiError(
                "AUDIO_EVIDENCE_REVIEW_FIELDS_FORBIDDEN",
                "音频证据人审只能写入录音归类和低置信覆盖层",
                422,
                details=[
                    {
                        "allowed_fields": sorted(allowed_fields),
                        "actual_fields": sorted(fields),
                    }
                ],
            )
        disposition = fields.get("recording_disposition")
        if disposition is not None and disposition not in {
            "main",
            "crosstalk",
            "duplicate",
        }:
            raise ApiError(
                "AUDIO_EVIDENCE_RECORDING_DISPOSITION_INVALID",
                "录音归类必须是 main、crosstalk 或 duplicate",
                422,
            )
        low_confidence = fields.get("low_confidence")
        if low_confidence is not None and not isinstance(low_confidence, bool):
            raise ApiError(
                "AUDIO_EVIDENCE_LOW_CONFIDENCE_INVALID",
                "低置信覆盖必须是布尔值",
                422,
            )
        return
    if target.collection == "label_candidates":
        allowed_fields = {"value", "value_or_action", "label", "reason", "confidence"}
        _reject_unexpected_review_fields(
            fields,
            allowed_fields=allowed_fields,
            code="LABEL_CANDIDATE_REVIEW_FIELDS_FORBIDDEN",
            message="标签候选人审只能修改值、标签、原因和置信度",
        )
        for field in ("value_or_action", "label", "reason"):
            if field in fields:
                _validate_review_text(fields[field], field=field, maximum=2000)
        if "confidence" in fields:
            _validate_review_confidence(fields["confidence"], field="confidence")
        return
    if target.collection == "event_links":
        allowed_fields = {
            "source_event_id",
            "document_ref",
            "relation_type",
            "confidence",
            "evidence_window",
        }
        _reject_unexpected_review_fields(
            fields,
            allowed_fields=allowed_fields,
            code="EVENT_LINK_REVIEW_FIELDS_FORBIDDEN",
            message="事件关联人审只能修改已声明的业务关联字段",
        )
        for field in ("source_event_id", "document_ref", "relation_type", "evidence_window"):
            if field in fields:
                _validate_review_text(fields[field], field=field)
        if "confidence" in fields:
            _validate_review_confidence(fields["confidence"], field="confidence")
        return
    if target.collection == "conversation_boundaries":
        allowed_fields = {
            "start_ms",
            "end_ms",
            "decision",
            "merged_slice_ids",
            "split_slice_ids",
            "extension_ids",
        }
        _reject_unexpected_review_fields(
            fields,
            allowed_fields=allowed_fields,
            code="CONVERSATION_BOUNDARY_REVIEW_FIELDS_FORBIDDEN",
            message="会话边界人审只能修改时间窗和受控切片引用",
        )
        start_ms = fields.get("start_ms")
        end_ms = fields.get("end_ms")
        if (
            isinstance(start_ms, bool)
            or not isinstance(start_ms, int)
            or isinstance(end_ms, bool)
            or not isinstance(end_ms, int)
            or start_ms < 0
            or end_ms <= start_ms
            or end_ms > 7 * 24 * 60 * 60 * 1000
        ):
            raise ApiError(
                "CONVERSATION_BOUNDARY_REVIEW_WINDOW_INVALID",
                "会话边界必须提供有效且有界的 start_ms/end_ms",
                422,
            )
        if "decision" in fields and fields["decision"] != "manual_confirmed":
            raise ApiError(
                "CONVERSATION_BOUNDARY_REVIEW_DECISION_INVALID",
                "会话边界人工决定必须是 manual_confirmed",
                422,
            )
        for field in ("merged_slice_ids", "split_slice_ids", "extension_ids"):
            if field in fields:
                _validate_review_identifier_list(fields[field], field=field)
        return
    if target.collection != "label_aggregates":
        return
    if set(fields) != {"value"}:
        raise ApiError(
            "LABEL_REVIEW_FIELDS_FORBIDDEN",
            "LabelAggregate 修改后接受只允许写 value",
            422,
            details=[{"allowed_fields": ["value"], "actual_fields": sorted(fields)}],
        )
    aggregate = session.scalar(
        select(LabelAggregate).where(
            LabelAggregate.aggregate_id == target.resource_key,
            LabelAggregate.tenant_id == ctx.tenant_id,
            LabelAggregate.project_id == ctx.project_id,
        )
    )
    if aggregate is None:
        raise ApiError("LABEL_AGGREGATE_PROJECTION_MISSING", "缺少 LabelAggregate 强表", 409)
    policy = session.scalar(
        select(LabelAggregationPolicyVersion).where(
            LabelAggregationPolicyVersion.policy_version_id == aggregate.policy_version_id,
            LabelAggregationPolicyVersion.tenant_id == ctx.tenant_id,
            LabelAggregationPolicyVersion.project_id == ctx.project_id,
        )
    )
    if policy is None:
        raise ApiError("LABEL_AGGREGATION_POLICY_MISSING", "缺少聚合策略强版本", 409)
    definition = next(
        (
            item
            for item in policy.label_definitions
            if str(item.get("label_id")) == aggregate.label_id
        ),
        None,
    )
    if definition is None:
        raise ApiError("LABEL_DEFINITION_MISSING", "聚合标签不属于锁定策略", 409)
    _validate_label_value(str(definition.get("kind") or aggregate.value_type), fields["value"])


def _add_by_key(
    session: Session,
    ctx: RequestContext,
    resources: dict[str, JsonResource],
    collection: str,
    resource_key: str,
) -> None:
    for resource in list_resources(session, ctx, collection, limit=200):
        if resource.resource_key == resource_key:
            resources[_target_key(resource)] = resource
            return


def _event_link_mentions_evidence_pack(data: dict[str, Any], evidence_pack_id: str) -> bool:
    if data.get("evidence_pack_id") == evidence_pack_id:
        return True
    for ref in data.get("evidence_refs") or []:
        if isinstance(ref, dict) and ref.get("evidence_pack_id") == evidence_pack_id:
            return True
    return False


def _apply_target_writeback(
    target: JsonResource,
    *,
    decision: Decision,
    decision_id: str,
    review_task_id: str,
    note: str | None,
    field_changes: dict[str, Any],
    ctx: RequestContext,
) -> None:
    status = TARGET_STATUS_BY_DECISION[decision]
    base = {
        "review_task_id": review_task_id,
        "review_decision_id": decision_id,
        "manual_decision": decision,
        "decision_note": note,
        "decided_by": ctx.user_id,
        "trace_id": ctx.trace_id,
    }
    if target.collection != "evidence_packs":
        target.data = {**target.data, **field_changes}
    if target.collection == "label_candidates":
        target.data = {
            **target.data,
            **base,
            "human_state": decision,
            "status": status,
        }
    elif target.collection == "event_links":
        target.data = {
            **target.data,
            **base,
            "relation_state": EVENT_RELATION_STATE_BY_DECISION[decision],
            "status": status,
            "review_note": note,
        }
    elif target.collection == "evidence_packs":
        review_overrides = target.data.get("review_overrides")
        target.data = {
            **target.data,
            **base,
            "review_state": decision,
            "review_overrides": {
                **(review_overrides if isinstance(review_overrides, dict) else {}),
                **field_changes,
            },
            # Evidence content and readiness are immutable. Human judgment is a
            # separate overlay and must never rewrite the evidence hash/status.
            "status": target.status,
        }
    elif target.collection == "conversation_boundaries":
        target.data = {
            **target.data,
            **base,
            "review_state": decision,
            "status": status,
        }
    elif target.collection == "voiceprint_samples":
        target.data = {
            **target.data,
            **base,
            "confirm_state": VOICEPRINT_STATE_BY_DECISION[decision],
            "status": status,
        }
    elif target.collection == "work_items" and target.data.get("insight_action_id"):
        action_status = {
            "accepted": "experiment_ready",
            "modified": "experiment_ready",
            "rejected": "blocked",
            "escalated": "pending_review",
        }[decision]
        target.data = {
            **target.data,
            **base,
            "review_state": decision,
            "status": action_status,
            "next_actions": [
                {
                    "key": "start_experiment",
                    "label": "创建效果实验",
                    "route": f"insights/actions/{target.resource_key}/experiments",
                }
            ]
            if action_status == "experiment_ready"
            else target.data.get("next_actions", []),
        }
        status = action_status
    else:
        target.data = {**target.data, **base, "status": status}
    if target.collection != "evidence_packs":
        target.status = status
    target.trace_id = ctx.trace_id


def _sync_insight_action_review(
    session: Session,
    ctx: RequestContext,
    target: JsonResource,
    decision: Decision,
) -> None:
    if target.collection != "work_items":
        return
    action_id = target.data.get("insight_action_id")
    if not isinstance(action_id, str) or not action_id:
        return
    action = session.get(InsightAction, action_id)
    if action is None or action.tenant_id != ctx.tenant_id or action.project_id != ctx.project_id:
        raise ApiError("INSIGHT_ACTION_PROJECTION_MISSING", "人审动作缺少强投影", 409)
    action.status = str(target.data.get("status") or action.status)
    action.resource_version += 1
    action.trace_id = ctx.trace_id
    action.payload = {
        **action.payload,
        "status": action.status,
        "review_state": decision,
        "review_decision_id": target.data.get("review_decision_id"),
        "review_task_id": target.data.get("review_task_id"),
        "resource_version": action.resource_version,
        "trace_id": ctx.trace_id,
        "next_actions": target.data.get("next_actions", action.payload.get("next_actions", [])),
    }


def _target_key(target: JsonResource) -> str:
    return f"{target.collection}:{target.resource_key}"


def _public_target_type(collection: str) -> str:
    return PUBLIC_TARGET_TYPE_BY_COLLECTION.get(collection, collection)
