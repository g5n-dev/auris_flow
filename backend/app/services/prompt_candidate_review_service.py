from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.models import (
    HumanReviewDecision,
    HumanReviewTask,
    JsonResource,
    PromptAsset,
    PromptVersion,
    PromptVersionCandidate,
)
from app.schemas.prompt_candidate_review import (
    PromptReviewAdjudicationRequest,
    PromptReviewSubmissionRequest,
)
from app.services.audit_service import record_audit
from app.services.label_closed_loop_service import materialize_human_review_feedback
from app.services.outbox_service import enqueue_event
from app.services.resource_service import upsert_resource

OPEN_CANDIDATE_STATES = frozenset({"candidate", "in-review"})
FINAL_CANDIDATE_STATUS = {
    "accepted": "approved",
    "modified": "revision-required",
    "rejected": "rejected",
}
FINAL_TASK_STATUS = {
    "accepted": "success",
    "modified": "blocked",
    "rejected": "blocked",
}


def _rooted_context(ctx: RequestContext, root_trace_id: str) -> RequestContext:
    if ctx.trace_id == root_trace_id:
        return ctx
    return replace(
        ctx,
        trace_id=root_trace_id,
        parent_trace_id=ctx.trace_id,
        correlation_id=root_trace_id,
    )


def submit_prompt_candidate_review(
    session: Session,
    ctx: RequestContext,
    candidate_id: str,
    body: PromptReviewSubmissionRequest,
) -> dict[str, Any]:
    bundle = _candidate_bundle_for_update(session, ctx, candidate_id)
    candidate, version, _, candidate_resource, task, task_resource = bundle
    if candidate.status not in OPEN_CANDIDATE_STATES:
        raise ApiError(
            "PROMPT_REVIEW_NOT_OPEN",
            "Prompt 候选已不接受新的盲审提交",
            409,
            details=[{"candidate_id": candidate_id, "status": candidate.status}],
        )
    if version.status != "candidate":
        raise ApiError(
            "PROMPT_VERSION_REVIEW_STATE_MISMATCH",
            "PromptVersion 与候选审核状态不一致",
            409,
            details=[{"prompt_version_status": version.status}],
        )

    submission_id = _scoped_id(
        "prs",
        ctx.tenant_id,
        ctx.project_id,
        candidate_id,
        ctx.user_id,
    )
    if (
        _json_resource_for_update(
            session,
            ctx,
            "prompt_review_submissions",
            submission_id,
        )
        is not None
    ):
        raise ApiError(
            "PROMPT_REVIEWER_ALREADY_SUBMITTED",
            "同一审核人只能为该 Prompt 候选提交一次密封结论",
            409,
        )

    submission_ids = _submission_ids(candidate.payload)
    if len(submission_ids) >= 2:
        raise ApiError(
            "PROMPT_REVIEW_SUBMISSION_LIMIT_REACHED",
            "该 Prompt 候选已收到两份密封结论",
            409,
        )
    submissions = _load_submissions(session, ctx, candidate_id, submission_ids)
    if any(item["reviewer_id"] == ctx.user_id for item in submissions):
        raise ApiError(
            "PROMPT_REVIEWER_ALREADY_SUBMITTED",
            "同一审核人只能为该 Prompt 候选提交一次密封结论",
            409,
        )

    now = datetime.now(UTC).isoformat()
    source_trace_id = _source_trace_id(candidate.payload, candidate.trace_id)
    submission_payload = {
        "id": submission_id,
        "submission_id": submission_id,
        "candidate_id": candidate_id,
        "review_task_id": task.review_task_id,
        "reviewer_id": ctx.user_id,
        "decision": body.decision,
        "note": body.note,
        "field_diff": body.model_dump(mode="json")["field_diff"],
        "status": "sealed",
        "submitted_at": now,
        "source_trace_id": source_trace_id,
        "trace_id": ctx.trace_id,
    }
    upsert_resource(
        session,
        ctx,
        "prompt_review_submissions",
        submission_id,
        submission_payload,
        status="sealed",
        trace_id=ctx.trace_id,
    )
    submission_ids.append(submission_id)
    submissions.append(submission_payload)
    record_audit(
        session,
        ctx,
        action="prompt_review.submission.created",
        object_type="prompt_review_submission",
        object_id=submission_id,
        # Audit proves that a sealed answer was persisted without making its decision or
        # field diff visible through Trace before the second reviewer submits.
        after={
            "submission_id": submission_id,
            "candidate_id": candidate_id,
            "review_task_id": task.review_task_id,
            "status": "sealed",
            "submitted_at": now,
        },
    )
    # The event intentionally excludes the sealed decision and diff. Those remain available
    # only to the review service until the pair is complete.
    enqueue_event(
        session,
        ctx,
        event_type="prompt_review.submission.created",
        aggregate_type="prompt_review_submission",
        aggregate_id=submission_id,
        payload={
            "submission_id": submission_id,
            "candidate_id": candidate_id,
            "review_task_id": task.review_task_id,
            "status": "sealed",
            "received_reviews": len(submission_ids),
        },
    )

    candidate_before = dict(candidate_resource.data)
    if len(submissions) == 1:
        _set_review_progress(
            session,
            ctx,
            candidate=candidate,
            candidate_resource=candidate_resource,
            task=task,
            task_resource=task_resource,
            submission_ids=submission_ids,
            candidate_status="in-review",
            task_status="pending",
            review_status="in-review",
        )
        return _submission_response(
            candidate_id=candidate_id,
            submission_id=submission_id,
            status="in-review",
            received_reviews=1,
            trace_id=ctx.trace_id,
            child_prompt_version_id=None,
        )

    if _submissions_agree(submissions):
        decision = str(submissions[0]["decision"])
        field_diff = dict(submissions[0].get("field_diff") or {})
        status = _finalize_review(
            session,
            ctx,
            candidate=candidate,
            version=version,
            candidate_resource=candidate_resource,
            task=task,
            task_resource=task_resource,
            candidate_before=candidate_before,
            decision=decision,
            note=body.note,
            field_diff=field_diff,
            submission_ids=submission_ids,
            resolution_source="reviewer-consensus",
            adjudication_id=None,
        )
    else:
        _set_review_progress(
            session,
            ctx,
            candidate=candidate,
            candidate_resource=candidate_resource,
            task=task,
            task_resource=task_resource,
            submission_ids=submission_ids,
            candidate_status="awaiting-adjudication",
            task_status="awaiting-adjudication",
            review_status="awaiting-adjudication",
        )
        status = "awaiting-adjudication"
        record_audit(
            session,
            ctx,
            action="prompt_review.adjudication_requested",
            object_type="prompt_version_candidate",
            object_id=candidate_id,
            before=candidate_before,
            after=candidate.payload,
        )
        enqueue_event(
            session,
            ctx,
            event_type="prompt_review.adjudication_requested",
            aggregate_type="prompt_version_candidate",
            aggregate_id=candidate_id,
            payload={
                "candidate_id": candidate_id,
                "review_task_id": task.review_task_id,
                "status": status,
                "submission_ids": submission_ids,
            },
        )

    return _submission_response(
        candidate_id=candidate_id,
        submission_id=submission_id,
        status=status,
        received_reviews=2,
        trace_id=ctx.trace_id,
        child_prompt_version_id=(
            str(candidate.payload["child_prompt_version_id"])
            if candidate.payload.get("child_prompt_version_id")
            else None
        ),
    )


def adjudicate_prompt_candidate_review(
    session: Session,
    ctx: RequestContext,
    candidate_id: str,
    body: PromptReviewAdjudicationRequest,
) -> dict[str, Any]:
    bundle = _candidate_bundle_for_update(session, ctx, candidate_id)
    candidate, version, _, candidate_resource, task, task_resource = bundle
    if candidate.status != "awaiting-adjudication":
        raise ApiError(
            "PROMPT_REVIEW_NOT_AWAITING_ADJUDICATION",
            "Prompt 候选当前不处于待仲裁状态",
            409,
            details=[{"candidate_id": candidate_id, "status": candidate.status}],
        )
    submission_ids = _submission_ids(candidate.payload)
    submissions = _load_submissions(session, ctx, candidate_id, submission_ids)
    if len(submissions) != 2:
        raise ApiError(
            "PROMPT_REVIEW_SUBMISSIONS_INCOMPLETE",
            "Prompt 候选必须先完成两份密封审核才能仲裁",
            409,
        )
    reviewer_ids = {str(item["reviewer_id"]) for item in submissions}
    if ctx.user_id in reviewer_ids:
        raise ApiError(
            "PROMPT_ADJUDICATOR_MUST_BE_INDEPENDENT",
            "仲裁人不能是该候选的任一盲审审核人",
            409,
        )

    adjudication_id = _scoped_id(
        "pra",
        ctx.tenant_id,
        ctx.project_id,
        candidate_id,
    )
    if (
        _json_resource_for_update(
            session,
            ctx,
            "prompt_review_adjudications",
            adjudication_id,
        )
        is not None
    ):
        raise ApiError(
            "PROMPT_REVIEW_ALREADY_ADJUDICATED",
            "该 Prompt 候选已经完成仲裁",
            409,
        )
    now = datetime.now(UTC).isoformat()
    field_diff = body.model_dump(mode="json")["field_diff"]
    adjudication_payload = {
        "id": adjudication_id,
        "adjudication_id": adjudication_id,
        "candidate_id": candidate_id,
        "review_task_id": task.review_task_id,
        "submission_ids": submission_ids,
        "adjudicator_id": ctx.user_id,
        "decision": body.decision,
        "reason": body.reason,
        "note": body.note,
        "field_diff": field_diff,
        "status": "resolved",
        "adjudicated_at": now,
        "source_trace_id": _source_trace_id(candidate.payload, candidate.trace_id),
        "trace_id": ctx.trace_id,
    }
    upsert_resource(
        session,
        ctx,
        "prompt_review_adjudications",
        adjudication_id,
        adjudication_payload,
        status="resolved",
        trace_id=ctx.trace_id,
    )
    record_audit(
        session,
        ctx,
        action="prompt_review.adjudication.created",
        object_type="prompt_review_adjudication",
        object_id=adjudication_id,
        after=adjudication_payload,
    )
    enqueue_event(
        session,
        ctx,
        event_type="prompt_review.adjudication.created",
        aggregate_type="prompt_review_adjudication",
        aggregate_id=adjudication_id,
        payload={
            "adjudication_id": adjudication_id,
            "candidate_id": candidate_id,
            "review_task_id": task.review_task_id,
            "decision": body.decision,
            "status": "resolved",
        },
    )
    status = _finalize_review(
        session,
        ctx,
        candidate=candidate,
        version=version,
        candidate_resource=candidate_resource,
        task=task,
        task_resource=task_resource,
        candidate_before=dict(candidate_resource.data),
        decision=body.decision,
        note=body.reason,
        field_diff=field_diff,
        submission_ids=submission_ids,
        resolution_source="adjudication",
        adjudication_id=adjudication_id,
    )
    return {
        "candidate_id": candidate_id,
        "adjudication_id": adjudication_id,
        "status": status,
        "received_reviews": 2,
        "trace_id": ctx.trace_id,
        **(
            {"child_prompt_version_id": candidate.payload["child_prompt_version_id"]}
            if candidate.payload.get("child_prompt_version_id")
            else {}
        ),
        "next_action": (
            "run-locked-evaluation"
            if status == "approved"
            else (
                "review-child-candidate"
                if candidate.payload.get("child_prompt_version_id")
                else "generate-new-candidate"
            )
        ),
    }


def _candidate_bundle_for_update(
    session: Session,
    ctx: RequestContext,
    candidate_id: str,
) -> tuple[
    PromptVersionCandidate,
    PromptVersion,
    PromptAsset,
    JsonResource,
    HumanReviewTask,
    JsonResource,
]:
    candidate = session.scalar(
        select(PromptVersionCandidate)
        .where(
            PromptVersionCandidate.candidate_id == candidate_id,
            PromptVersionCandidate.tenant_id == ctx.tenant_id,
            PromptVersionCandidate.project_id == ctx.project_id,
        )
        .with_for_update()
    )
    if candidate is None:
        raise ApiError("NOT_FOUND", f"prompt_version_candidates 不存在：{candidate_id}", 404)
    version = session.scalar(
        select(PromptVersion)
        .where(
            PromptVersion.prompt_version_id == candidate_id,
            PromptVersion.tenant_id == ctx.tenant_id,
            PromptVersion.project_id == ctx.project_id,
        )
        .with_for_update()
    )
    if version is None:
        raise ApiError(
            "PROMPT_VERSION_PROJECTION_MISSING",
            "Prompt 候选缺少强 PromptVersion",
            409,
        )
    asset = session.scalar(
        select(PromptAsset)
        .where(
            PromptAsset.prompt_asset_id == version.prompt_asset_id,
            PromptAsset.tenant_id == ctx.tenant_id,
            PromptAsset.project_id == ctx.project_id,
        )
        .with_for_update()
    )
    if asset is None:
        raise ApiError("PROMPT_ASSET_PROJECTION_MISSING", "Prompt 候选缺少 PromptAsset", 409)
    candidate_resource = _json_resource_for_update(
        session,
        ctx,
        "prompt_version_candidates",
        candidate_id,
    )
    if candidate_resource is None:
        raise ApiError(
            "PROMPT_CANDIDATE_PROJECTION_MISSING",
            "Prompt 候选强表与兼容投影不一致",
            409,
        )
    review_task_id = candidate.payload.get("review_task_id")
    if not isinstance(review_task_id, str) or not review_task_id:
        raise ApiError(
            "PROMPT_REVIEW_TASK_BINDING_MISSING",
            "Prompt 候选未绑定独立审核任务",
            409,
        )
    task = session.scalar(
        select(HumanReviewTask)
        .where(
            HumanReviewTask.review_task_id == review_task_id,
            HumanReviewTask.tenant_id == ctx.tenant_id,
            HumanReviewTask.project_id == ctx.project_id,
        )
        .with_for_update()
    )
    task_resource = _json_resource_for_update(
        session,
        ctx,
        "human_review_tasks",
        review_task_id,
    )
    if task is None or task_resource is None:
        raise ApiError(
            "PROMPT_REVIEW_TASK_PROJECTION_MISSING",
            "Prompt 候选审核任务强表与业务投影不一致",
            409,
        )
    _validate_task_binding(task_resource.data, candidate_id)
    return candidate, version, asset, candidate_resource, task, task_resource


def _validate_task_binding(task_data: dict[str, Any], candidate_id: str) -> None:
    target_refs = task_data.get("target_refs") or []
    expected = [{"type": "prompt_version_candidate", "id": candidate_id}]
    if (
        task_data.get("queue") != "prompt_approval"
        or task_data.get("review_mode") != "double-blind"
        or task_data.get("required_reviews") != 2
        or target_refs != expected
    ):
        raise ApiError(
            "PROMPT_REVIEW_TASK_BINDING_INVALID",
            "Prompt 候选必须独立绑定一个双盲审核任务",
            409,
        )


def _json_resource_for_update(
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


def _submission_ids(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("review_submission_ids") or []
    if not isinstance(raw, list) or any(not isinstance(item, str) or not item for item in raw):
        raise ApiError(
            "PROMPT_REVIEW_SUBMISSION_REFERENCES_INVALID",
            "Prompt 候选的密封审核引用无效",
            409,
        )
    if len(raw) != len(set(raw)):
        raise ApiError(
            "PROMPT_REVIEW_SUBMISSION_REFERENCES_INVALID",
            "Prompt 候选的密封审核引用重复",
            409,
        )
    return list(raw)


def _load_submissions(
    session: Session,
    ctx: RequestContext,
    candidate_id: str,
    submission_ids: list[str],
) -> list[dict[str, Any]]:
    submissions: list[dict[str, Any]] = []
    for submission_id in submission_ids:
        resource = _json_resource_for_update(
            session,
            ctx,
            "prompt_review_submissions",
            submission_id,
        )
        if resource is None or resource.data.get("candidate_id") != candidate_id:
            raise ApiError(
                "PROMPT_REVIEW_SUBMISSION_PROJECTION_MISSING",
                "Prompt 候选的密封审核记录不完整",
                409,
            )
        submissions.append(dict(resource.data))
    return submissions


def _submissions_agree(submissions: list[dict[str, Any]]) -> bool:
    if len(submissions) != 2:
        return False
    if submissions[0].get("decision") != submissions[1].get("decision"):
        return False
    if submissions[0].get("decision") != "modified":
        return True
    return _canonical_json(submissions[0].get("field_diff") or {}) == _canonical_json(
        submissions[1].get("field_diff") or {}
    )


def _set_review_progress(
    session: Session,
    ctx: RequestContext,
    *,
    candidate: PromptVersionCandidate,
    candidate_resource: JsonResource,
    task: HumanReviewTask,
    task_resource: JsonResource,
    submission_ids: list[str],
    candidate_status: str,
    task_status: str,
    review_status: str,
) -> None:
    candidate_payload = {
        **candidate.payload,
        "status": candidate_status,
        "review_status": review_status,
        "review_submission_ids": submission_ids,
        "received_reviews": len(submission_ids),
        "source_trace_id": _source_trace_id(candidate.payload, candidate.trace_id),
        "trace_id": ctx.trace_id,
    }
    candidate.status = candidate_status
    candidate.trace_id = ctx.trace_id
    candidate.payload = candidate_payload
    upsert_resource(
        session,
        ctx,
        "prompt_version_candidates",
        candidate.candidate_id,
        candidate_payload,
        status=candidate_status,
        trace_id=ctx.trace_id,
    )
    task_payload = {
        **task_resource.data,
        "status": task_status,
        "review_status": review_status,
        "received_reviews": len(submission_ids),
        "source_trace_id": _source_trace_id(candidate.payload, candidate.trace_id),
        "trace_id": ctx.trace_id,
    }
    task.status = task_status
    task.trace_id = ctx.trace_id
    task.payload = task_payload
    upsert_resource(
        session,
        ctx,
        "human_review_tasks",
        task.review_task_id,
        task_payload,
        status=task_status,
        trace_id=ctx.trace_id,
    )


def _prompt_field_value(version: PromptVersion, field: str) -> dict[str, Any]:
    values = {
        "template": version.template_json,
        "output_schema": version.output_schema,
        "generation_params": version.generation_params,
    }
    return dict(values[field])


def _materialize_modified_prompt_candidate(
    session: Session,
    ctx: RequestContext,
    *,
    candidate: PromptVersionCandidate,
    version: PromptVersion,
    decision_id: str,
    field_diff: dict[str, Any],
) -> dict[str, str]:
    next_values = {
        "template": dict(version.template_json),
        "output_schema": dict(version.output_schema),
        "generation_params": dict(version.generation_params),
    }
    for field, change in sorted(field_diff.items()):
        current = _prompt_field_value(version, field)
        if _canonical_json(change.get("before")) != _canonical_json(current):
            raise ApiError(
                "PROMPT_REVIEW_DIFF_BASE_MISMATCH",
                "Prompt 修改 diff 的 before 与被审核版本不一致",
                409,
                details=[{"field": field}],
            )
        after = change.get("after")
        if not isinstance(after, dict) or (field == "output_schema" and not after):
            raise ApiError(
                "PROMPT_REVIEW_DIFF_VALUE_INVALID",
                "Prompt template、output_schema 与 generation_params 必须保持对象结构",
                422,
                details=[{"field": field}],
            )
        if _canonical_json(after) == _canonical_json(current):
            raise ApiError(
                "PROMPT_REVIEW_DIFF_NOOP",
                "Prompt 修改 diff 必须产生实际内容变化",
                422,
                details=[{"field": field}],
            )
        next_values[field] = after

    source_trace_id = _source_trace_id(candidate.payload, candidate.trace_id)
    content_document = {
        "prompt_asset_id": version.prompt_asset_id,
        "parent_version_id": version.prompt_version_id,
        "label_version_id": version.label_version_id,
        "schema_version": version.schema_version,
        "model_version": version.model_version,
        "template": next_values["template"],
        "output_schema": next_values["output_schema"],
        "generation_params": next_values["generation_params"],
    }
    content_sha256 = hashlib.sha256(_canonical_json(content_document).encode("utf-8")).hexdigest()
    duplicate = session.scalar(
        select(PromptVersion.prompt_version_id).where(
            PromptVersion.tenant_id == ctx.tenant_id,
            PromptVersion.project_id == ctx.project_id,
            PromptVersion.content_sha256 == content_sha256,
        )
    )
    if duplicate is not None:
        raise ApiError(
            "PROMPT_REVIEW_REVISION_CONTENT_CONFLICT",
            "修改后的 Prompt 内容已存在，不能创建重复候选",
            409,
            details=[{"prompt_version_id": duplicate}],
        )
    child_id = _scoped_id(
        "pv",
        ctx.tenant_id,
        ctx.project_id,
        version.prompt_version_id,
        decision_id,
        content_sha256,
    )
    if session.get(PromptVersion, child_id) is not None:
        raise ApiError(
            "PROMPT_REVIEW_REVISION_ID_CONFLICT",
            "修改后的 child PromptVersion 已存在",
            409,
        )
    child_version = PromptVersion(
        prompt_version_id=child_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        prompt_asset_id=version.prompt_asset_id,
        version=f"revision-{content_sha256[:12]}",
        parent_version_id=version.prompt_version_id,
        label_version_id=version.label_version_id,
        schema_version=version.schema_version,
        model_version=version.model_version,
        status="candidate",
        template_json=next_values["template"],
        output_schema=next_values["output_schema"],
        generation_params=next_values["generation_params"],
        structured_diff=field_diff,
        source_badcase_refs=list(version.source_badcase_refs),
        content_sha256=content_sha256,
        trace_id=ctx.trace_id,
    )
    session.add(child_version)
    review_task_id = (
        "hrt_"
        + hashlib.sha256(_canonical_json(["prompt", child_id]).encode("utf-8")).hexdigest()[:24]
    )
    child_payload = {
        "id": child_id,
        "candidate_id": child_id,
        "prompt_version_id": child_id,
        "prompt_asset_id": version.prompt_asset_id,
        "parent_version_id": version.prompt_version_id,
        "revision_of_candidate_id": candidate.candidate_id,
        "source_review_decision_id": decision_id,
        "label_version_id": version.label_version_id,
        "model_version": version.model_version,
        "schema_version": version.schema_version,
        "status": "candidate",
        "template": next_values["template"],
        "output_schema": next_values["output_schema"],
        "generation_params": next_values["generation_params"],
        "structured_diff": field_diff,
        "source_badcase_refs": list(version.source_badcase_refs),
        "content_sha256": content_sha256,
        "review_task_id": review_task_id,
        "review_gate": {
            "required": True,
            "mode": "double-blind",
            "required_reviews": 2,
            "requires_adjudication_on_disagreement": True,
        },
        "source_trace_id": source_trace_id,
        "trace_id": ctx.trace_id,
    }
    session.add(
        PromptVersionCandidate(
            candidate_id=child_id,
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            status="candidate",
            trace_id=ctx.trace_id,
            payload=child_payload,
        )
    )
    upsert_resource(
        session,
        ctx,
        "prompt_version_candidates",
        child_id,
        child_payload,
        status="candidate",
        trace_id=ctx.trace_id,
        audit_action="prompt_version_candidate.revision_created",
    )
    task_payload = {
        "id": review_task_id,
        "review_task_id": review_task_id,
        "status": "pending",
        "review_status": "pending",
        "queue": "prompt_approval",
        "risk_level": "high",
        "review_mode": "double-blind",
        "required_reviews": 2,
        "target_refs": [{"type": "prompt_version_candidate", "id": child_id}],
        "revision_of_candidate_id": candidate.candidate_id,
        "source_review_decision_id": decision_id,
        "source_trace_id": source_trace_id,
        "trace_id": ctx.trace_id,
    }
    upsert_resource(
        session,
        ctx,
        "human_review_tasks",
        review_task_id,
        task_payload,
        status="pending",
        trace_id=ctx.trace_id,
        audit_action="human_review_task.prompt_revision_created",
    )
    record_audit(
        session,
        ctx,
        action="prompt_version.revision_materialized",
        object_type="prompt_version",
        object_id=child_id,
        after=child_payload,
    )
    enqueue_event(
        session,
        ctx,
        event_type="prompt_version_candidate.revision_created",
        aggregate_type="prompt_version_candidate",
        aggregate_id=child_id,
        payload=child_payload,
    )
    enqueue_event(
        session,
        ctx,
        event_type="human_review_task.created",
        aggregate_type="human_review_task",
        aggregate_id=review_task_id,
        payload=task_payload,
    )
    return {
        "child_prompt_version_id": child_id,
        "child_review_task_id": review_task_id,
    }


def _finalize_review(
    session: Session,
    ctx: RequestContext,
    *,
    candidate: PromptVersionCandidate,
    version: PromptVersion,
    candidate_resource: JsonResource,
    task: HumanReviewTask,
    task_resource: JsonResource,
    candidate_before: dict[str, Any],
    decision: str,
    note: str | None,
    field_diff: dict[str, Any],
    submission_ids: list[str],
    resolution_source: str,
    adjudication_id: str | None,
) -> str:
    action_trace_id = ctx.trace_id
    source_trace_id = _source_trace_id(candidate.payload, candidate.trace_id) or ctx.trace_id
    ctx = _rooted_context(ctx, source_trace_id)
    status = FINAL_CANDIDATE_STATUS[decision]
    task_status = FINAL_TASK_STATUS[decision]
    now = datetime.now(UTC).isoformat()
    decision_id = _scoped_id(
        "hrd",
        ctx.tenant_id,
        ctx.project_id,
        candidate.candidate_id,
        "prompt-double-blind",
    )
    revision = (
        _materialize_modified_prompt_candidate(
            session,
            ctx,
            candidate=candidate,
            version=version,
            decision_id=decision_id,
            field_diff=field_diff,
        )
        if decision == "modified"
        else {}
    )
    candidate_payload = {
        **candidate.payload,
        "status": status,
        "review_status": decision,
        "review_submission_ids": submission_ids,
        "received_reviews": 2,
        "review_decision_id": decision_id,
        "review_resolution_source": resolution_source,
        "adjudication_id": adjudication_id,
        "requested_field_diff": field_diff if decision == "modified" else {},
        **revision,
        "reviewed_at": now,
        "source_trace_id": source_trace_id,
        "trace_id": ctx.trace_id,
        "action_trace_id": action_trace_id,
    }
    candidate.status = status
    candidate.trace_id = ctx.trace_id
    candidate.payload = candidate_payload
    version.status = status
    version.trace_id = ctx.trace_id
    target_resource = upsert_resource(
        session,
        ctx,
        "prompt_version_candidates",
        candidate.candidate_id,
        candidate_payload,
        status=status,
        trace_id=ctx.trace_id,
    )
    task_payload = {
        **task_resource.data,
        "status": task_status,
        "review_status": decision,
        "decision": decision,
        "decision_id": decision_id,
        "decision_note": note,
        "decided_at": now,
        "decided_by": "double-blind-consensus"
        if resolution_source == "reviewer-consensus"
        else ctx.user_id,
        "received_reviews": 2,
        "adjudication_id": adjudication_id,
        "source_trace_id": source_trace_id,
        "trace_id": ctx.trace_id,
        "action_trace_id": action_trace_id,
    }
    task.status = task_status
    task.trace_id = ctx.trace_id
    task.payload = task_payload
    upsert_resource(
        session,
        ctx,
        "human_review_tasks",
        task.review_task_id,
        task_payload,
        status=task_status,
        trace_id=ctx.trace_id,
    )
    decision_payload = {
        "id": decision_id,
        "decision_id": decision_id,
        "review_task_id": task.review_task_id,
        "decision": decision,
        "status": task_status,
        "note": note,
        "decided_by": task_payload["decided_by"],
        "decided_at": now,
        "source": resolution_source,
        "submission_ids": submission_ids,
        "adjudication_id": adjudication_id,
        "source_trace_id": source_trace_id,
        "affected_objects": [
            {"type": "human_review_task", "id": task.review_task_id},
            {"type": "prompt_version_candidate", "id": candidate.candidate_id},
            {"type": "prompt_version", "id": version.prompt_version_id},
            *(
                [
                    {
                        "type": "prompt_version_candidate",
                        "id": revision["child_prompt_version_id"],
                    },
                    {
                        "type": "human_review_task",
                        "id": revision["child_review_task_id"],
                    },
                ]
                if revision
                else []
            ),
        ],
        "trace_id": ctx.trace_id,
        "action_trace_id": action_trace_id,
    }
    # The strong decision row supplies the database-level exactly-once terminal constraint
    # for the review task without exposing either sealed answer.
    session.add(
        HumanReviewDecision(
            decision_id=decision_id,
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            review_task_id=task.review_task_id,
            terminal_review_task_id=task.review_task_id,
            status=task_status,
            trace_id=ctx.trace_id,
            payload=decision_payload,
        )
    )
    session.flush()
    upsert_resource(
        session,
        ctx,
        "human_review_decisions",
        decision_id,
        decision_payload,
        status=task_status,
        trace_id=ctx.trace_id,
    )
    session.flush()
    materialize_human_review_feedback(
        session,
        ctx,
        decision_id=decision_id,
        review_task_id=task.review_task_id,
        decision=decision,
        note=note,
        target_resources=[target_resource],
        target_befores={f"prompt_version_candidates:{candidate.candidate_id}": candidate_before},
    )
    record_audit(
        session,
        ctx,
        action="prompt_review.resolved",
        object_type="prompt_version_candidate",
        object_id=candidate.candidate_id,
        before=candidate_before,
        after=candidate_payload,
    )
    enqueue_event(
        session,
        ctx,
        event_type="human_review.decision.created",
        aggregate_type="human_review_decision",
        aggregate_id=decision_id,
        # Keep sealed submission IDs and reviewer notes in the strong decision row.
        # The event is a least-privilege integration fact that remains visible in
        # governed traces without disclosing either blind review answer.
        payload={
            "decision_id": decision_id,
            "review_task_id": task.review_task_id,
            "prompt_version_candidate_id": candidate.candidate_id,
            "prompt_version_id": version.prompt_version_id,
            "decision": decision,
            "status": task_status,
            "resolution_source": resolution_source,
            "adjudication_id": adjudication_id,
        },
    )
    enqueue_event(
        session,
        ctx,
        event_type="prompt_review.resolved",
        aggregate_type="prompt_version_candidate",
        aggregate_id=candidate.candidate_id,
        payload={
            "candidate_id": candidate.candidate_id,
            "prompt_version_id": version.prompt_version_id,
            "review_task_id": task.review_task_id,
            "review_decision_id": decision_id,
            "decision": decision,
            "status": status,
            "resolution_source": resolution_source,
            "adjudication_id": adjudication_id,
            **revision,
        },
    )
    return status


def _submission_response(
    *,
    candidate_id: str,
    submission_id: str,
    status: str,
    received_reviews: int,
    trace_id: str,
    child_prompt_version_id: str | None,
) -> dict[str, Any]:
    response = {
        "candidate_id": candidate_id,
        "submission_id": submission_id,
        "submission_status": "sealed",
        "status": status,
        "received_reviews": received_reviews,
        "required_reviews": 2,
        "trace_id": trace_id,
        "next_action": (
            "review-child-candidate"
            if status == "revision-required" and child_prompt_version_id
            else {
                "in-review": "await-second-review",
                "awaiting-adjudication": "await-adjudication",
                "approved": "run-locked-evaluation",
                "rejected": "generate-new-candidate",
                "revision-required": "generate-new-candidate",
            }[status]
        ),
    }
    if child_prompt_version_id:
        response["child_prompt_version_id"] = child_prompt_version_id
    return response


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _scoped_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256(_canonical_json(parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def _source_trace_id(payload: dict[str, Any], fallback: str | None) -> str | None:
    value = payload.get("source_trace_id") or payload.get("trace_id") or fallback
    return str(value) if value else None
