from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.models import (
    FeedbackExample,
    HumanReviewTask,
    JsonResource,
    LabelAggregate,
    LabelAggregationRun,
    LabelNode,
    LabelTaxonomySuggestion,
    LabelVersion,
    LabelVersionItem,
)
from app.schemas.label_closed_loop import (
    ClosedLoopReviewAdjudicationRequest,
    ClosedLoopReviewSubmissionRequest,
)
from app.services.audit_service import record_audit
from app.services.human_review_service import apply_human_review_decision
from app.services.label_closed_loop_service import canonical_sha256
from app.services.label_lifecycle_compat_service import label_version_item_definition_sha256
from app.services.outbox_service import enqueue_event
from app.services.resource_service import upsert_resource

TargetKind = Literal["aggregate", "taxonomy"]


@dataclass(slots=True)
class _ReviewBundle:
    kind: TargetKind
    target_id: str
    target: LabelAggregate | LabelTaxonomySuggestion
    target_resource: JsonResource
    task: HumanReviewTask
    task_resource: JsonResource


def _rooted_context(ctx: RequestContext, root_trace_id: str) -> RequestContext:
    if ctx.trace_id == root_trace_id:
        return ctx
    return replace(
        ctx,
        trace_id=root_trace_id,
        parent_trace_id=ctx.trace_id,
        correlation_id=root_trace_id,
    )


def submit_closed_loop_review(
    session: Session,
    ctx: RequestContext,
    *,
    kind: TargetKind,
    target_id: str,
    body: ClosedLoopReviewSubmissionRequest,
) -> dict[str, Any]:
    bundle = _review_bundle_for_update(session, ctx, kind=kind, target_id=target_id)
    _validate_request_for_target(session, ctx, bundle, body)
    if bundle.task.status not in {"pending"}:
        raise ApiError(
            "CLOSED_LOOP_REVIEW_NOT_OPEN",
            "闭环双盲任务当前不接受新的审核提交",
            409,
            details=[{"review_task_id": bundle.task.review_task_id, "status": bundle.task.status}],
        )

    submission_ids = _submission_ids(bundle.task.payload)
    submissions = _load_submissions(session, ctx, bundle, submission_ids)
    if any(item["reviewer_id"] == ctx.user_id for item in submissions):
        raise ApiError(
            "CLOSED_LOOP_REVIEWER_ALREADY_SUBMITTED",
            "同一审核人只能提交一次密封结论",
            409,
        )
    if len(submissions) >= 2:
        raise ApiError(
            "CLOSED_LOOP_REVIEW_SUBMISSION_LIMIT_REACHED",
            "闭环任务已收到两份独立审核结论",
            409,
        )

    submission_id = _scoped_id(
        "clrs",
        ctx.tenant_id,
        ctx.project_id,
        kind,
        target_id,
        ctx.user_id,
    )
    now = datetime.now(UTC).isoformat()
    submission = {
        "id": submission_id,
        "submission_id": submission_id,
        "target_kind": kind,
        "target_id": target_id,
        "review_task_id": bundle.task.review_task_id,
        "reviewer_id": ctx.user_id,
        **body.model_dump(mode="json", exclude_none=True),
        "status": "sealed",
        "submitted_at": now,
        "trace_id": ctx.trace_id,
    }
    upsert_resource(
        session,
        ctx,
        "closed_loop_review_submissions",
        submission_id,
        submission,
        status="sealed",
        trace_id=ctx.trace_id,
    )
    submission_ids.append(submission_id)
    submissions.append(submission)
    _set_task_progress(
        session,
        ctx,
        bundle,
        submission_ids=submission_ids,
        review_status="in-review",
        task_status="pending",
    )
    record_audit(
        session,
        ctx,
        action="closed_loop_review.submission.created",
        object_type="closed_loop_review_submission",
        object_id=submission_id,
        after={
            "submission_id": submission_id,
            "target_kind": kind,
            "target_id": target_id,
            "review_task_id": bundle.task.review_task_id,
            "status": "sealed",
        },
    )
    enqueue_event(
        session,
        ctx,
        event_type="closed_loop_review.submission.created",
        aggregate_type="closed_loop_review_submission",
        aggregate_id=submission_id,
        payload={
            "submission_id": submission_id,
            "target_kind": kind,
            "target_id": target_id,
            "review_task_id": bundle.task.review_task_id,
            "status": "sealed",
            "received_reviews": len(submissions),
        },
    )

    if len(submissions) == 1:
        return _response(bundle, submission_id, "in-review", 1, ctx.trace_id)
    if _review_payload(submissions[0]) == _review_payload(submissions[1]):
        status = _finalize(
            session,
            ctx,
            bundle,
            resolution=_review_payload(submissions[0]),
            submission_ids=submission_ids,
            resolution_source="reviewer-consensus",
            adjudication_id=None,
        )
    else:
        _set_task_progress(
            session,
            ctx,
            bundle,
            submission_ids=submission_ids,
            review_status="awaiting-adjudication",
            task_status="awaiting-adjudication",
        )
        status = "awaiting-adjudication"
        record_audit(
            session,
            ctx,
            action="closed_loop_review.adjudication_requested",
            object_type=_object_type(bundle),
            object_id=target_id,
            after={
                "review_task_id": bundle.task.review_task_id,
                "submission_ids": submission_ids,
                "status": status,
            },
        )
        enqueue_event(
            session,
            ctx,
            event_type="closed_loop_review.adjudication_requested",
            aggregate_type=_object_type(bundle),
            aggregate_id=target_id,
            payload={
                "review_task_id": bundle.task.review_task_id,
                "submission_ids": submission_ids,
                "status": status,
            },
        )
    return _response(bundle, submission_id, status, 2, ctx.trace_id)


def adjudicate_closed_loop_review(
    session: Session,
    ctx: RequestContext,
    *,
    kind: TargetKind,
    target_id: str,
    body: ClosedLoopReviewAdjudicationRequest,
) -> dict[str, Any]:
    bundle = _review_bundle_for_update(session, ctx, kind=kind, target_id=target_id)
    _validate_request_for_target(session, ctx, bundle, body)
    if bundle.task.status != "awaiting-adjudication":
        raise ApiError(
            "CLOSED_LOOP_REVIEW_NOT_AWAITING_ADJUDICATION",
            "闭环任务当前不处于待仲裁状态",
            409,
        )
    submission_ids = _submission_ids(bundle.task.payload)
    submissions = _load_submissions(session, ctx, bundle, submission_ids)
    if len(submissions) != 2:
        raise ApiError(
            "CLOSED_LOOP_REVIEW_SUBMISSIONS_INCOMPLETE",
            "必须先收到两份独立密封审核才能仲裁",
            409,
        )
    reviewer_ids = {str(item["reviewer_id"]) for item in submissions}
    if ctx.user_id in reviewer_ids:
        raise ApiError(
            "CLOSED_LOOP_ADJUDICATOR_MUST_BE_INDEPENDENT",
            "仲裁人不能是两名盲审审核人之一",
            409,
        )
    adjudication_id = _scoped_id(
        "clra",
        ctx.tenant_id,
        ctx.project_id,
        kind,
        target_id,
    )
    if _resource_for_update(session, ctx, "closed_loop_review_adjudications", adjudication_id):
        raise ApiError(
            "CLOSED_LOOP_REVIEW_ALREADY_ADJUDICATED",
            "该闭环任务已经完成仲裁",
            409,
        )
    adjudication = {
        "id": adjudication_id,
        "adjudication_id": adjudication_id,
        "target_kind": kind,
        "target_id": target_id,
        "review_task_id": bundle.task.review_task_id,
        "submission_ids": submission_ids,
        "adjudicator_id": ctx.user_id,
        **body.model_dump(mode="json", exclude_none=True),
        "status": "resolved",
        "adjudicated_at": datetime.now(UTC).isoformat(),
        "trace_id": ctx.trace_id,
    }
    upsert_resource(
        session,
        ctx,
        "closed_loop_review_adjudications",
        adjudication_id,
        adjudication,
        status="resolved",
        trace_id=ctx.trace_id,
    )
    record_audit(
        session,
        ctx,
        action="closed_loop_review.adjudication.created",
        object_type="closed_loop_review_adjudication",
        object_id=adjudication_id,
        after=adjudication,
    )
    enqueue_event(
        session,
        ctx,
        event_type="closed_loop_review.adjudication.created",
        aggregate_type="closed_loop_review_adjudication",
        aggregate_id=adjudication_id,
        payload={
            "adjudication_id": adjudication_id,
            "target_kind": kind,
            "target_id": target_id,
            "review_task_id": bundle.task.review_task_id,
            "status": "resolved",
        },
    )
    resolution = body.model_dump(mode="json", exclude={"reason"}, exclude_none=True)
    status = _finalize(
        session,
        ctx,
        bundle,
        resolution=resolution,
        submission_ids=submission_ids,
        resolution_source="adjudication",
        adjudication_id=adjudication_id,
    )
    return {
        "target_kind": kind,
        "target_id": target_id,
        "review_task_id": bundle.task.review_task_id,
        "adjudication_id": adjudication_id,
        "status": status,
        "received_reviews": 2,
        "trace_id": ctx.trace_id,
        **(
            {"candidate_label_version_id": bundle.target.payload["candidate_label_version_id"]}
            if bundle.kind == "taxonomy"
            and isinstance(bundle.target, LabelTaxonomySuggestion)
            and bundle.target.payload.get("candidate_label_version_id")
            else {}
        ),
    }


def _review_bundle_for_update(
    session: Session,
    ctx: RequestContext,
    *,
    kind: TargetKind,
    target_id: str,
) -> _ReviewBundle:
    target: LabelAggregate | LabelTaxonomySuggestion | None
    if kind == "aggregate":
        target = session.scalar(
            select(LabelAggregate)
            .where(
                LabelAggregate.aggregate_id == target_id,
                LabelAggregate.tenant_id == ctx.tenant_id,
                LabelAggregate.project_id == ctx.project_id,
            )
            .with_for_update()
        )
        collection = "label_aggregates"
        expected_type = "label_aggregate"
        if target is not None and target.risk_level != "high":
            raise ApiError(
                "CLOSED_LOOP_DOUBLE_BLIND_NOT_REQUIRED",
                "只有高风险 Aggregate 使用专用双盲接口",
                409,
            )
    else:
        target = session.scalar(
            select(LabelTaxonomySuggestion)
            .where(
                LabelTaxonomySuggestion.suggestion_id == target_id,
                LabelTaxonomySuggestion.tenant_id == ctx.tenant_id,
                LabelTaxonomySuggestion.project_id == ctx.project_id,
            )
            .with_for_update()
        )
        collection = "label_taxonomy_suggestions"
        expected_type = "taxonomy_suggestion"
    if target is None:
        raise ApiError("NOT_FOUND", f"闭环人审目标不存在：{target_id}", 404)
    review_task_id = target.review_task_id
    if not review_task_id:
        raise ApiError(
            "CLOSED_LOOP_REVIEW_TASK_BINDING_MISSING",
            "闭环目标未绑定独立审核任务",
            409,
        )
    target_resource = _resource_for_update(session, ctx, collection, target_id)
    task = session.scalar(
        select(HumanReviewTask)
        .where(
            HumanReviewTask.review_task_id == review_task_id,
            HumanReviewTask.tenant_id == ctx.tenant_id,
            HumanReviewTask.project_id == ctx.project_id,
        )
        .with_for_update()
    )
    task_resource = _resource_for_update(session, ctx, "human_review_tasks", str(review_task_id))
    if target_resource is None or task is None or task_resource is None:
        raise ApiError(
            "CLOSED_LOOP_REVIEW_PROJECTION_MISSING",
            "闭环目标或审核任务强表与业务投影不一致",
            409,
        )
    expected_refs = [{"type": expected_type, "id": target_id}]
    if (
        task_resource.data.get("target_refs") != expected_refs
        or task.payload.get("target_refs") != expected_refs
        or task_resource.data.get("review_mode") != "double-blind"
        or task_resource.data.get("required_reviews") != 2
    ):
        raise ApiError(
            "CLOSED_LOOP_REVIEW_TASK_BINDING_INVALID",
            "闭环双盲任务必须一任务一对象并与强表 review_task_id 一致",
            409,
        )
    return _ReviewBundle(kind, target_id, target, target_resource, task, task_resource)


def _validate_request_for_target(
    session: Session,
    ctx: RequestContext,
    bundle: _ReviewBundle,
    body: ClosedLoopReviewSubmissionRequest,
) -> None:
    fields_set = body.model_fields_set
    if bundle.kind == "aggregate":
        if body.taxonomy_action is not None or body.canonical_target_label_id is not None:
            raise ApiError(
                "AGGREGATE_REVIEW_TAXONOMY_FIELDS_FORBIDDEN",
                "Aggregate 审核不能提交 Taxonomy 字段",
                422,
            )
        if body.decision == "modified" and "value" not in fields_set:
            raise ApiError(
                "AGGREGATE_MODIFIED_VALUE_REQUIRED",
                "Aggregate 修改后接受必须显式提交 value",
                422,
            )
        if body.decision != "modified" and "value" in fields_set:
            raise ApiError(
                "AGGREGATE_VALUE_CHANGE_NOT_ALLOWED",
                "只有修改后接受可以提交 Aggregate value",
                422,
            )
        return
    if "value" in fields_set:
        raise ApiError(
            "TAXONOMY_REVIEW_VALUE_FORBIDDEN",
            "Taxonomy 审核不能修改 Aggregate value",
            422,
        )
    if body.taxonomy_action is None:
        raise ApiError(
            "TAXONOMY_REVIEW_ACTION_REQUIRED",
            "Taxonomy 审核必须给出 alias/create/merge/split/reject 动作",
            422,
        )
    if body.taxonomy_action in {"alias", "merge"}:
        assert isinstance(bundle.target, LabelTaxonomySuggestion)
        canonical_target = session.scalar(
            select(LabelVersionItem).where(
                LabelVersionItem.tenant_id == ctx.tenant_id,
                LabelVersionItem.project_id == ctx.project_id,
                LabelVersionItem.label_version_id == bundle.target.label_version_id,
                LabelVersionItem.label_id == body.canonical_target_label_id,
                LabelVersionItem.status == "active",
            )
        )
        if canonical_target is None:
            raise ApiError(
                "TAXONOMY_CANONICAL_TARGET_NOT_FOUND",
                "alias/merge 的 canonical target 必须来自当前锁定标签版本",
                409,
            )


def _set_task_progress(
    session: Session,
    ctx: RequestContext,
    bundle: _ReviewBundle,
    *,
    submission_ids: list[str],
    review_status: str,
    task_status: str,
) -> None:
    bundle.task_resource.data = {
        **bundle.task_resource.data,
        "review_submission_ids": submission_ids,
        "received_reviews": len(submission_ids),
        "review_status": review_status,
        "status": task_status,
        "trace_id": ctx.trace_id,
    }
    bundle.task_resource.status = task_status
    bundle.task_resource.trace_id = ctx.trace_id
    bundle.task.status = task_status
    bundle.task.trace_id = ctx.trace_id
    bundle.task.payload = {
        **bundle.task.payload,
        "review_submission_ids": submission_ids,
        "received_reviews": len(submission_ids),
        "review_status": review_status,
        "status": task_status,
        "trace_id": ctx.trace_id,
    }


def _finalize(
    session: Session,
    ctx: RequestContext,
    bundle: _ReviewBundle,
    *,
    resolution: dict[str, Any],
    submission_ids: list[str],
    resolution_source: str,
    adjudication_id: str | None,
) -> str:
    action_trace_id = ctx.trace_id
    root_trace_id = str(
        bundle.task_resource.data.get("source_trace_id")
        or bundle.task_resource.data.get("trace_id")
        or bundle.target.trace_id
        or ctx.trace_id
    )
    ctx = _rooted_context(ctx, root_trace_id)
    decision = str(resolution["decision"])
    note = resolution.get("note")
    _set_task_progress(
        session,
        ctx,
        bundle,
        submission_ids=submission_ids,
        review_status="pending",
        task_status="pending",
    )
    if bundle.kind == "aggregate":
        changes = (
            [
                {
                    "target_type": "label_aggregate",
                    "target_id": bundle.target_id,
                    "fields": {"value": resolution.get("value")},
                }
            ]
            if decision == "modified"
            else []
        )
        generic_decision = decision
    else:
        taxonomy_action = str(resolution["taxonomy_action"])
        changes = (
            []
            if decision == "rejected"
            else [
                {
                    "target_type": "taxonomy_suggestion",
                    "target_id": bundle.target_id,
                    "fields": {
                        "proposed_action": taxonomy_action,
                        "canonical_target_label_id": resolution.get("canonical_target_label_id"),
                    },
                }
            ]
        )
        generic_decision = "rejected" if decision == "rejected" else "modified"
    result = apply_human_review_decision(
        session,
        ctx,
        task=bundle.task_resource,
        task_projection=bundle.task,
        request_body={
            "decision": generic_decision,
            "note": note,
            "reason": note,
            "changes": changes,
            "_action_trace_id": action_trace_id,
        },
    )
    decision_id = str(result["decision_id"])
    _promote_feedback_to_gold(session, ctx, decision_id=decision_id)
    _sync_strong_target(
        session,
        ctx,
        bundle,
        decision=decision,
        resolution=resolution,
        decision_id=decision_id,
    )
    _complete_aggregation_run(session, ctx, review_task_id=bundle.task.review_task_id)
    bundle.task_resource.data = {
        **bundle.task_resource.data,
        "review_submission_ids": submission_ids,
        "received_reviews": 2,
        "review_mode": "double-blind",
        "resolution_source": resolution_source,
        "adjudication_id": adjudication_id,
        "gold_status": "gold",
    }
    bundle.task.payload = {
        **bundle.task.payload,
        "review_submission_ids": submission_ids,
        "received_reviews": 2,
        "review_mode": "double-blind",
        "resolution_source": resolution_source,
        "adjudication_id": adjudication_id,
        "gold_status": "gold",
    }
    record_audit(
        session,
        ctx,
        action="closed_loop_review.completed",
        object_type=_object_type(bundle),
        object_id=bundle.target_id,
        after={
            "decision_id": decision_id,
            "decision": decision,
            "review_task_id": bundle.task.review_task_id,
            "submission_ids": submission_ids,
            "resolution_source": resolution_source,
            "adjudication_id": adjudication_id,
            "gold_status": "gold",
            "trace_id": ctx.trace_id,
            "action_trace_id": action_trace_id,
        },
    )
    enqueue_event(
        session,
        ctx,
        event_type="closed_loop_review.completed",
        aggregate_type=_object_type(bundle),
        aggregate_id=bundle.target_id,
        payload={
            "decision_id": decision_id,
            "decision": decision,
            "review_task_id": bundle.task.review_task_id,
            "submission_ids": submission_ids,
            "resolution_source": resolution_source,
            "adjudication_id": adjudication_id,
            "gold_status": "gold",
            "trace_id": ctx.trace_id,
            "action_trace_id": action_trace_id,
            **(
                {"candidate_label_version_id": bundle.target.payload["candidate_label_version_id"]}
                if bundle.kind == "taxonomy"
                and isinstance(bundle.target, LabelTaxonomySuggestion)
                and bundle.target.payload.get("candidate_label_version_id")
                else {}
            ),
        },
    )
    return "accepted" if decision in {"accepted", "modified"} else "rejected"


def _promote_feedback_to_gold(
    session: Session,
    ctx: RequestContext,
    *,
    decision_id: str,
) -> None:
    feedback_records = list(
        session.scalars(
            select(FeedbackExample).where(
                FeedbackExample.tenant_id == ctx.tenant_id,
                FeedbackExample.project_id == ctx.project_id,
                FeedbackExample.review_decision_id == decision_id,
            )
        )
    )
    if len(feedback_records) != 1:
        raise ApiError(
            "CLOSED_LOOP_FEEDBACK_CARDINALITY_INVALID",
            "闭环双盲终态必须且只能生成一个目标反馈样本",
            409,
        )
    feedback = feedback_records[0]
    before = feedback.gold_status
    feedback.gold_status = "gold"
    feedback.trace_id = ctx.trace_id
    record_audit(
        session,
        ctx,
        action="feedback_example.gold_promoted",
        object_type="feedback_example",
        object_id=feedback.feedback_example_id,
        before={"gold_status": before},
        after={"gold_status": "gold", "review_decision_id": decision_id},
    )
    enqueue_event(
        session,
        ctx,
        event_type="feedback_example.gold_promoted",
        aggregate_type="feedback_example",
        aggregate_id=feedback.feedback_example_id,
        payload={
            "feedback_example_id": feedback.feedback_example_id,
            "review_decision_id": decision_id,
            "gold_status": "gold",
        },
    )


def _taxonomy_aliases(suggestion: LabelTaxonomySuggestion) -> list[str]:
    values = [suggestion.normalized_label, *suggestion.raw_labels]
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = str(value).strip()
        key = normalized.casefold()
        if normalized and key not in seen:
            seen.add(key)
            result.append(normalized)
    return result


def _taxonomy_candidate_item_document(item: LabelVersionItem) -> dict[str, Any]:
    return {
        "label_id": item.label_id,
        "canonical_name": item.canonical_name,
        "aliases": item.aliases,
        "value_type": item.value_type,
        "risk_level": item.risk_level,
        "mutual_exclusion_group": item.mutual_exclusion_group,
        "parent_ids": item.parent_ids,
        "aggregation_rule": item.aggregation_rule,
        "status": item.status,
    }


def _materialize_taxonomy_candidate(
    session: Session,
    ctx: RequestContext,
    *,
    suggestion: LabelTaxonomySuggestion,
    action: str,
    canonical_target_label_id: str | None,
    decision_id: str,
) -> dict[str, Any]:
    parent = session.scalar(
        select(LabelVersion)
        .where(
            LabelVersion.label_version_id == suggestion.label_version_id,
            LabelVersion.tenant_id == ctx.tenant_id,
            LabelVersion.project_id == ctx.project_id,
        )
        .with_for_update()
    )
    if parent is None:
        raise ApiError(
            "TAXONOMY_PARENT_LABEL_VERSION_NOT_FOUND",
            "Taxonomy suggestion 绑定的父 LabelVersion 不存在",
            409,
        )
    parent_items = list(
        session.scalars(
            select(LabelVersionItem)
            .where(
                LabelVersionItem.tenant_id == ctx.tenant_id,
                LabelVersionItem.project_id == ctx.project_id,
                LabelVersionItem.label_version_id == parent.label_version_id,
            )
            .order_by(LabelVersionItem.label_id)
        )
    )
    candidate_id = (
        "lv_"
        + canonical_sha256(
            [
                ctx.tenant_id,
                ctx.project_id,
                suggestion.suggestion_id,
                decision_id,
                action,
                canonical_target_label_id,
            ]
        )[:24]
    )
    if session.get(LabelVersion, candidate_id) is not None:
        raise ApiError(
            "TAXONOMY_CANDIDATE_LABEL_VERSION_CONFLICT",
            "Taxonomy 候选 LabelVersion 已存在",
            409,
        )

    aliases = _taxonomy_aliases(suggestion)
    blockers: list[dict[str, Any]] = []
    created_label_id: str | None = None
    item_documents: list[dict[str, Any]] = []
    for source in parent_items:
        cloned_aliases = list(source.aliases)
        if action in {"alias", "merge"} and source.label_id == canonical_target_label_id:
            existing = {str(value).casefold() for value in cloned_aliases}
            cloned_aliases.extend(value for value in aliases if value.casefold() not in existing)
        item_documents.append(
            {
                **_taxonomy_candidate_item_document(source),
                "aliases": cloned_aliases,
            }
        )

    if action in {"alias", "merge"} and not any(
        item["label_id"] == canonical_target_label_id for item in item_documents
    ):
        raise ApiError(
            "TAXONOMY_CANONICAL_TARGET_NOT_FOUND",
            "Taxonomy 候选的 canonical target 不在父 LabelVersion 中",
            409,
        )
    if action == "create":
        created_label_id = (
            "label_"
            + canonical_sha256([ctx.tenant_id, ctx.project_id, suggestion.normalized_label])[:20]
        )
        node = session.scalar(
            select(LabelNode).where(
                LabelNode.tenant_id == ctx.tenant_id,
                LabelNode.project_id == ctx.project_id,
                LabelNode.label_id == created_label_id,
            )
        )
        if node is None:
            node_payload = {
                "source_taxonomy_suggestion_id": suggestion.suggestion_id,
                "source_review_decision_id": decision_id,
                "configuration_status": "pending",
            }
            node = LabelNode(
                node_id="ln_"
                + canonical_sha256([ctx.tenant_id, ctx.project_id, created_label_id])[:24],
                tenant_id=ctx.tenant_id,
                project_id=ctx.project_id,
                label_id=created_label_id,
                canonical_name=suggestion.normalized_label,
                status="active",
                trace_id=ctx.trace_id,
                payload=node_payload,
            )
            session.add(node)
            record_audit(
                session,
                ctx,
                action="label_node.taxonomy_candidate_created",
                object_type="label_node",
                object_id=created_label_id,
                after={
                    "label_id": created_label_id,
                    "canonical_name": suggestion.normalized_label,
                    **node_payload,
                },
            )
            enqueue_event(
                session,
                ctx,
                event_type="label_node.taxonomy_candidate_created",
                aggregate_type="label_node",
                aggregate_id=created_label_id,
                payload={
                    "label_id": created_label_id,
                    "canonical_name": suggestion.normalized_label,
                    **node_payload,
                },
            )
        configuration_blockers = [
            "VALUE_TYPE_CONFIRMATION_REQUIRED",
            "AGGREGATION_RULE_REQUIRED",
        ]
        blockers.extend(
            {"code": code, "label_id": created_label_id} for code in configuration_blockers
        )
        item_documents.append(
            {
                "label_id": created_label_id,
                "canonical_name": suggestion.normalized_label,
                "aliases": aliases,
                "value_type": "categorical",
                "risk_level": "high",
                "mutual_exclusion_group": None,
                "parent_ids": [],
                "aggregation_rule": {
                    "configuration_status": "pending",
                    "blockers": configuration_blockers,
                },
                "status": "pending-configuration",
            }
        )
    elif action == "split":
        blockers.extend(
            [
                {"code": "SPLIT_CHILD_LABELS_REQUIRED"},
                {"code": "SPLIT_ROUTING_RULE_REQUIRED"},
            ]
        )

    item_documents.sort(key=lambda item: str(item["label_id"]))
    change_set = {
        "action": action,
        "normalized_label": suggestion.normalized_label,
        "raw_labels": suggestion.raw_labels,
        "canonical_target_label_id": canonical_target_label_id,
        "created_label_id": created_label_id,
    }
    manifest_sha256 = canonical_sha256(
        {
            "parent_label_version_id": parent.label_version_id,
            "change_set": change_set,
            "items": item_documents,
            "blockers": blockers,
        }
    )
    candidate_payload = {
        "id": candidate_id,
        "label_version_id": candidate_id,
        "version": f"taxonomy-candidate-{manifest_sha256[:8]}",
        "status": "draft",
        "resource_version": 1,
        "parent_label_version_id": parent.label_version_id,
        "taxonomy_id": parent.taxonomy_id,
        "parent_resource_version": parent.resource_version,
        "source_taxonomy_suggestion_id": suggestion.suggestion_id,
        "source_review_decision_id": decision_id,
        "change_set": change_set,
        "blockers": blockers,
        "ready_for_publish": False,
        "cloned_item_count": len(parent_items),
        "item_count": len(item_documents),
        "manifest_sha256": manifest_sha256,
        "trace_id": ctx.trace_id,
    }
    upsert_resource(
        session,
        ctx,
        "label_versions",
        candidate_id,
        candidate_payload,
        status="draft",
        trace_id=ctx.trace_id,
        audit_action="label_version.taxonomy_candidate_created",
    )
    session.flush()
    candidate = session.get(LabelVersion, candidate_id)
    if candidate is None:
        raise ApiError(
            "TAXONOMY_CANDIDATE_LABEL_VERSION_MISSING",
            "Taxonomy 候选 LabelVersion 强投影物化失败",
            500,
        )
    candidate.policy_version_id = parent.policy_version_id
    candidate.payload = {**candidate.payload, **candidate_payload}
    for item in item_documents:
        version_item = LabelVersionItem(
            label_version_item_id="lvi_" + canonical_sha256([candidate_id, item["label_id"]])[:24],
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            label_version_id=candidate_id,
            label_id=str(item["label_id"]),
            canonical_name=str(item["canonical_name"]),
            aliases=deepcopy(item["aliases"]),
            value_type=str(item["value_type"]),
            risk_level=str(item["risk_level"]),
            mutual_exclusion_group=item["mutual_exclusion_group"],
            parent_ids=deepcopy(item["parent_ids"]),
            aggregation_rule=deepcopy(item["aggregation_rule"]),
            status=str(item["status"]),
            trace_id=ctx.trace_id,
        )
        version_item.definition_sha256 = label_version_item_definition_sha256(version_item)
        session.add(version_item)
    record_audit(
        session,
        ctx,
        action="label_version.taxonomy_candidate_materialized",
        object_type="label_version",
        object_id=candidate_id,
        after=candidate_payload,
    )
    enqueue_event(
        session,
        ctx,
        event_type="label_version.taxonomy_candidate_created",
        aggregate_type="label_version",
        aggregate_id=candidate_id,
        payload=candidate_payload,
    )
    return {
        "candidate_label_version_id": candidate_id,
        "candidate_manifest_sha256": manifest_sha256,
        **({"created_label_id": created_label_id} if created_label_id else {}),
    }


def _sync_strong_target(
    session: Session,
    ctx: RequestContext,
    bundle: _ReviewBundle,
    *,
    decision: str,
    resolution: dict[str, Any],
    decision_id: str,
) -> None:
    if bundle.kind == "aggregate":
        assert isinstance(bundle.target, LabelAggregate)
        before = {
            "status": bundle.target.status,
            "review_task_id": bundle.target.review_task_id,
        }
        bundle.target.status = "accepted" if decision in {"accepted", "modified"} else "rejected"
        bundle.target.review_task_id = None
        bundle.target.trace_id = ctx.trace_id
        bundle.target_resource.data = {
            **bundle.target_resource.data,
            "status": bundle.target.status,
            "review_task_id": None,
            "gold_status": "gold",
            "trace_id": ctx.trace_id,
        }
        bundle.target_resource.status = bundle.target.status
        after = {
            "status": bundle.target.status,
            "review_task_id": None,
            "review_decision_id": decision_id,
        }
        object_type = "label_aggregate"
        event_type = "label_aggregate.review_completed"
    else:
        assert isinstance(bundle.target, LabelTaxonomySuggestion)
        before = {
            "status": bundle.target.status,
            "proposed_action": bundle.target.proposed_action,
            "canonical_target_label_id": bundle.target.canonical_target_label_id,
            "review_task_id": bundle.target.review_task_id,
        }
        action = str(resolution.get("taxonomy_action") or "reject")
        materialized = (
            _materialize_taxonomy_candidate(
                session,
                ctx,
                suggestion=bundle.target,
                action=action,
                canonical_target_label_id=resolution.get("canonical_target_label_id"),
                decision_id=decision_id,
            )
            if decision != "rejected"
            else {}
        )
        bundle.target.status = "accepted" if decision != "rejected" else "rejected"
        bundle.target.proposed_action = action
        bundle.target.canonical_target_label_id = resolution.get("canonical_target_label_id")
        bundle.target.review_task_id = None
        bundle.target.trace_id = ctx.trace_id
        bundle.target.payload = {
            **bundle.target.payload,
            "review_decision_id": decision_id,
            "gold_status": "gold",
            **materialized,
        }
        bundle.target_resource.data = {
            **bundle.target_resource.data,
            "status": bundle.target.status,
            "proposed_action": action,
            "canonical_target_label_id": bundle.target.canonical_target_label_id,
            "review_task_id": None,
            "gold_status": "gold",
            **materialized,
            "trace_id": ctx.trace_id,
        }
        bundle.target_resource.status = bundle.target.status
        after = {
            "status": bundle.target.status,
            "proposed_action": action,
            "canonical_target_label_id": bundle.target.canonical_target_label_id,
            "review_task_id": None,
            "review_decision_id": decision_id,
            **materialized,
        }
        object_type = "label_taxonomy_suggestion"
        event_type = "label_taxonomy_suggestion.review_completed"
    bundle.target_resource.trace_id = ctx.trace_id
    record_audit(
        session,
        ctx,
        action=event_type,
        object_type=object_type,
        object_id=bundle.target_id,
        before=before,
        after=after,
    )
    enqueue_event(
        session,
        ctx,
        event_type=event_type,
        aggregate_type=object_type,
        aggregate_id=bundle.target_id,
        payload=after,
    )


def _complete_aggregation_run(
    session: Session,
    ctx: RequestContext,
    *,
    review_task_id: str,
) -> None:
    runs = list(
        session.scalars(
            select(LabelAggregationRun)
            .where(
                LabelAggregationRun.tenant_id == ctx.tenant_id,
                LabelAggregationRun.project_id == ctx.project_id,
            )
            .with_for_update()
        )
    )
    matching = [run for run in runs if review_task_id in (run.payload.get("review_task_ids") or [])]
    if len(matching) != 1:
        raise ApiError(
            "CLOSED_LOOP_AGGREGATION_RUN_BINDING_INVALID",
            "闭环审核任务必须且只能绑定一个聚合运行",
            409,
        )
    run = matching[0]
    before = {
        "status": run.status,
        "review_task_ids": list(run.payload.get("review_task_ids") or []),
    }
    remaining = [item for item in before["review_task_ids"] if str(item) != review_task_id]
    run.status = "awaiting-review" if remaining else "completed"
    run.trace_id = ctx.trace_id
    run.payload = {
        **run.payload,
        "status": run.status,
        "review_task_ids": remaining,
        "completed_at": datetime.now(UTC).isoformat() if not remaining else None,
    }
    after = {"status": run.status, "review_task_ids": remaining}
    record_audit(
        session,
        ctx,
        action="label_aggregation_run.review_progressed",
        object_type="label_aggregation_run",
        object_id=run.aggregation_run_id,
        before=before,
        after=after,
    )
    enqueue_event(
        session,
        ctx,
        event_type=(
            "label_aggregation_run.completed"
            if not remaining
            else "label_aggregation_run.review_progressed"
        ),
        aggregate_type="label_aggregation_run",
        aggregate_id=run.aggregation_run_id,
        payload=after,
    )


def _load_submissions(
    session: Session,
    ctx: RequestContext,
    bundle: _ReviewBundle,
    submission_ids: list[str],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for submission_id in submission_ids:
        resource = _resource_for_update(
            session, ctx, "closed_loop_review_submissions", submission_id
        )
        if (
            resource is None
            or resource.data.get("target_kind") != bundle.kind
            or resource.data.get("target_id") != bundle.target_id
            or resource.data.get("review_task_id") != bundle.task.review_task_id
            or resource.data.get("status") != "sealed"
        ):
            raise ApiError(
                "CLOSED_LOOP_REVIEW_SUBMISSION_BINDING_INVALID",
                "密封审核提交与闭环任务绑定不一致",
                409,
            )
        records.append(dict(resource.data))
    return records


def _submission_ids(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("review_submission_ids") or []
    if (
        not isinstance(raw, list)
        or len(raw) != len(set(raw))
        or any(not isinstance(item, str) or not item for item in raw)
    ):
        raise ApiError(
            "CLOSED_LOOP_REVIEW_SUBMISSION_REFERENCES_INVALID",
            "闭环任务的审核提交引用无效",
            409,
        )
    return list(raw)


def _review_payload(submission: dict[str, Any]) -> dict[str, Any]:
    return {
        key: submission[key]
        for key in (
            "decision",
            "value",
            "taxonomy_action",
            "canonical_target_label_id",
        )
        if key in submission
    }


def _resource_for_update(
    session: Session,
    ctx: RequestContext,
    collection: str,
    resource_key: str,
) -> JsonResource | None:
    return session.scalar(
        select(JsonResource)
        .where(
            JsonResource.collection == collection,
            JsonResource.resource_key == resource_key,
            JsonResource.tenant_id == ctx.tenant_id,
            JsonResource.project_id == ctx.project_id,
        )
        .with_for_update()
    )


def _scoped_id(prefix: str, *parts: str) -> str:
    return f"{prefix}_{canonical_sha256(list(parts))[:24]}"


def _object_type(bundle: _ReviewBundle) -> str:
    return "label_aggregate" if bundle.kind == "aggregate" else "label_taxonomy_suggestion"


def _response(
    bundle: _ReviewBundle,
    submission_id: str,
    status: str,
    received_reviews: int,
    trace_id: str,
) -> dict[str, Any]:
    response = {
        "target_kind": bundle.kind,
        "target_id": bundle.target_id,
        "review_task_id": bundle.task.review_task_id,
        "submission_id": submission_id,
        "status": status,
        "received_reviews": received_reviews,
        "trace_id": trace_id,
    }
    if (
        bundle.kind == "taxonomy"
        and isinstance(bundle.target, LabelTaxonomySuggestion)
        and bundle.target.payload.get("candidate_label_version_id")
    ):
        response["candidate_label_version_id"] = bundle.target.payload["candidate_label_version_id"]
    return response


__all__ = ["adjudicate_closed_loop_review", "submit_closed_loop_review"]
