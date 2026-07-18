from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.models import HumanReviewDecision, HumanReviewTask, JsonResource, QualityAppeal
from app.services.audit_service import record_audit
from app.services.label_review_projection_service import sync_label_review_projection
from app.services.outbox_service import enqueue_event
from app.services.resource_service import upsert_resource

QUALITY_APPEAL_STATUSES = frozenset({"submitted", "under_review", "resolved", "withdrawn"})
SOURCE_TERMINAL_DECISIONS = frozenset(
    {"accepted", "approved", "confirm", "modified", "rejected", "blocked"}
)


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def quality_appeal_data(appeal: QualityAppeal) -> dict[str, Any]:
    return {
        "id": appeal.appeal_id,
        "appeal_id": appeal.appeal_id,
        "source_decision_id": appeal.source_decision_id,
        "source_review_task_id": appeal.source_review_task_id,
        "review_task_id": appeal.review_task_id,
        "appeal_decision_id": appeal.appeal_decision_id,
        "source_result_sha256": appeal.source_result_sha256,
        "source_decider_id": appeal.source_decider_id,
        "source_trace_id": appeal.source_trace_id,
        "root_trace_id": appeal.root_trace_id,
        "current_trace_id": appeal.current_trace_id,
        "appellant_id": appeal.appellant_id,
        "evidence_refs": list(appeal.evidence_refs or []),
        "reason": appeal.reason,
        "status": appeal.status,
        "reviewer_id": appeal.reviewer_id,
        "decision": appeal.decision,
        "decision_reason": appeal.decision_reason,
        "withdrawal_reason": appeal.withdrawal_reason,
        "resource_version": appeal.resource_version,
        "claimed_at": _isoformat(appeal.claimed_at),
        "resolved_at": _isoformat(appeal.resolved_at),
        "withdrawn_at": _isoformat(appeal.withdrawn_at),
        "created_at": _isoformat(appeal.created_at),
        "updated_at": _isoformat(appeal.updated_at),
    }


def _source_snapshot(decision: HumanReviewDecision) -> dict[str, Any]:
    return {
        "decision_id": decision.decision_id,
        "review_task_id": decision.review_task_id,
        "terminal_review_task_id": decision.terminal_review_task_id,
        "status": decision.status,
        "trace_id": decision.trace_id,
        "payload": decision.payload,
    }


def _snapshot_sha256(snapshot: dict[str, Any]) -> str:
    canonical = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _source_decision_for_update(
    session: Session,
    ctx: RequestContext,
    source_decision_id: str,
) -> HumanReviewDecision:
    decision = session.scalar(
        select(HumanReviewDecision)
        .where(
            HumanReviewDecision.decision_id == source_decision_id,
            HumanReviewDecision.tenant_id == ctx.tenant_id,
            HumanReviewDecision.project_id == ctx.project_id,
        )
        .with_for_update()
    )
    if decision is None:
        raise ApiError(
            "QUALITY_APPEAL_SOURCE_NOT_FOUND",
            f"终态人审裁决不存在：{source_decision_id}",
            404,
        )
    source_kind = str((decision.payload or {}).get("decision") or "")
    if decision.terminal_review_task_id is None or source_kind not in SOURCE_TERMINAL_DECISIONS:
        raise ApiError(
            "QUALITY_APPEAL_SOURCE_NOT_TERMINAL",
            "仅终态 HumanReviewDecision 可以发起质检申诉",
            422,
        )
    return decision


def create_quality_appeal(
    session: Session,
    ctx: RequestContext,
    request_body: dict[str, Any],
) -> dict[str, Any]:
    source = _source_decision_for_update(
        session,
        ctx,
        str(request_body["source_decision_id"]),
    )
    existing = session.scalar(
        select(QualityAppeal)
        .where(
            QualityAppeal.tenant_id == ctx.tenant_id,
            QualityAppeal.project_id == ctx.project_id,
            QualityAppeal.source_decision_id == source.decision_id,
        )
        .with_for_update()
    )
    if existing is not None:
        raise ApiError(
            "QUALITY_APPEAL_ALREADY_EXISTS",
            "该终态人审裁决已经存在申诉",
            409,
        )

    source_payload = source.payload or {}
    source_decider_id = str(source_payload.get("decided_by") or "").strip()
    if not source_decider_id:
        raise ApiError(
            "QUALITY_APPEAL_SOURCE_DECIDER_MISSING",
            "源人审裁决缺少 decided_by，无法执行回避校验",
            409,
        )
    source_trace_id = str(source.trace_id or source_payload.get("trace_id") or "").strip()
    if not source_trace_id:
        raise ApiError(
            "QUALITY_APPEAL_SOURCE_TRACE_MISSING",
            "源人审裁决缺少 trace_id，无法冻结申诉证据链",
            409,
        )
    root_trace_id = str(
        source_payload.get("root_trace_id")
        or source_payload.get("source_trace_id")
        or source_trace_id
    )
    appeal_token = uuid.uuid4().hex[:16]
    appeal_id = f"qap_{appeal_token}"
    review_task_id = f"hrt_qap_{appeal_token}"
    review_task_payload = {
        "id": review_task_id,
        "review_task_id": review_task_id,
        "queue": "quality_appeal",
        "title": "单案质检申诉",
        "appeal_id": appeal_id,
        "source_decision_id": source.decision_id,
        "source_review_task_id": str(source.terminal_review_task_id),
        "source_result_sha256": _snapshot_sha256(_source_snapshot(source)),
        "source_decider_id": source_decider_id,
        "appellant_id": ctx.user_id,
        "evidence_refs": list(request_body["evidence_refs"]),
        "reason": str(request_body["reason"]),
        "status": "submitted",
        "reviewer_id": None,
        "appeal_decision_id": None,
        "resource_version": 1,
        "root_trace_id": root_trace_id,
        "source_trace_id": source_trace_id,
        "current_trace_id": ctx.trace_id,
        "trace_id": ctx.trace_id,
    }
    upsert_resource(
        session,
        ctx,
        "human_review_tasks",
        review_task_id,
        review_task_payload,
        status="submitted",
        trace_id=ctx.trace_id,
        audit_action="quality_appeal.review_task.created",
    )
    session.flush()

    appeal = QualityAppeal(
        appeal_id=appeal_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        source_decision_id=source.decision_id,
        source_review_task_id=str(source.terminal_review_task_id),
        review_task_id=review_task_id,
        appeal_decision_id=None,
        source_result_sha256=review_task_payload["source_result_sha256"],
        source_decider_id=source_decider_id,
        source_trace_id=source_trace_id,
        root_trace_id=root_trace_id,
        current_trace_id=ctx.trace_id,
        appellant_id=ctx.user_id,
        evidence_refs=list(request_body["evidence_refs"]),
        reason=str(request_body["reason"]),
        status="submitted",
        resource_version=1,
    )
    session.add(appeal)
    session.flush()
    after = quality_appeal_data(appeal)
    record_audit(
        session,
        ctx,
        action="quality_appeal.submitted",
        object_type="quality_appeal",
        object_id=appeal.appeal_id,
        after=after,
    )
    enqueue_event(
        session,
        ctx,
        event_type="quality_appeal.submitted",
        aggregate_type="quality_appeal",
        aggregate_id=appeal.appeal_id,
        payload=after,
    )
    return after


def get_quality_appeal(
    session: Session,
    ctx: RequestContext,
    appeal_id: str,
    *,
    for_update: bool = False,
) -> QualityAppeal:
    statement = select(QualityAppeal).where(
        QualityAppeal.appeal_id == appeal_id,
        QualityAppeal.tenant_id == ctx.tenant_id,
        QualityAppeal.project_id == ctx.project_id,
    )
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    appeal = session.scalar(statement)
    if appeal is None:
        raise ApiError("QUALITY_APPEAL_NOT_FOUND", f"质检申诉不存在：{appeal_id}", 404)
    return appeal


def list_quality_appeals(
    session: Session,
    ctx: RequestContext,
    *,
    status: str | None = None,
    source_decision_id: str | None = None,
    appellant_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    if status is not None and status not in QUALITY_APPEAL_STATUSES:
        raise ApiError("QUALITY_APPEAL_STATUS_INVALID", "质检申诉状态筛选值无效", 422)
    statement = select(QualityAppeal).where(
        QualityAppeal.tenant_id == ctx.tenant_id,
        QualityAppeal.project_id == ctx.project_id,
    )
    if status is not None:
        statement = statement.where(QualityAppeal.status == status)
    if source_decision_id is not None:
        statement = statement.where(QualityAppeal.source_decision_id == source_decision_id)
    if appellant_id is not None:
        statement = statement.where(QualityAppeal.appellant_id == appellant_id)
    appeals = session.scalars(
        statement.order_by(QualityAppeal.created_at.desc(), QualityAppeal.appeal_id.desc()).limit(
            limit
        )
    ).all()
    return [quality_appeal_data(appeal) for appeal in appeals]


def _assert_expected_version(appeal: QualityAppeal, expected_resource_version: int) -> None:
    if appeal.resource_version == expected_resource_version:
        return
    raise ApiError(
        "QUALITY_APPEAL_VERSION_CONFLICT",
        "申诉已被其他请求更新，请刷新后重试",
        409,
        details=[
            {
                "expected_resource_version": expected_resource_version,
                "current_resource_version": appeal.resource_version,
            }
        ],
    )


def _assert_reviewer_separation(appeal: QualityAppeal, ctx: RequestContext) -> None:
    if ctx.user_id == appeal.appellant_id:
        raise ApiError(
            "QUALITY_APPEAL_SELF_REVIEW_FORBIDDEN",
            "申诉申请人不能审核自己的申诉",
            403,
        )
    if ctx.user_id == appeal.source_decider_id:
        raise ApiError(
            "QUALITY_APPEAL_ORIGINAL_DECIDER_REVIEW_FORBIDDEN",
            "原人审裁决人不能审核该裁决的申诉",
            403,
        )


def _appeal_review_task_for_update(
    session: Session,
    ctx: RequestContext,
    appeal: QualityAppeal,
) -> tuple[HumanReviewTask, JsonResource]:
    task = session.scalar(
        select(HumanReviewTask)
        .where(
            HumanReviewTask.review_task_id == appeal.review_task_id,
            HumanReviewTask.tenant_id == ctx.tenant_id,
            HumanReviewTask.project_id == ctx.project_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    resource = session.scalar(
        select(JsonResource)
        .where(
            JsonResource.collection == "human_review_tasks",
            JsonResource.resource_key == appeal.review_task_id,
            JsonResource.tenant_id == ctx.tenant_id,
            JsonResource.project_id == ctx.project_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if task is None or resource is None:
        raise ApiError(
            "QUALITY_APPEAL_REVIEW_TASK_MISSING",
            "申诉领取任务强表或业务投影缺失",
            409,
        )
    task_payload = task.payload or {}
    resource_payload = resource.data or {}
    for payload in (task_payload, resource_payload):
        if payload.get("queue") != "quality_appeal" or payload.get("appeal_id") != appeal.appeal_id:
            raise ApiError(
                "QUALITY_APPEAL_REVIEW_TASK_MISMATCH",
                "申诉领取任务未绑定当前申诉",
                409,
            )
    if task.status != appeal.status or resource.status != appeal.status:
        raise ApiError(
            "QUALITY_APPEAL_REVIEW_TASK_INCONSISTENT",
            "申诉与领取任务状态不一致",
            409,
        )
    if (
        task_payload.get("reviewer_id") != appeal.reviewer_id
        or resource_payload.get("reviewer_id") != appeal.reviewer_id
    ):
        raise ApiError(
            "QUALITY_APPEAL_REVIEWER_INCONSISTENT",
            "申诉与领取任务 reviewer 不一致",
            409,
        )
    return task, resource


def _update_appeal_review_task(
    session: Session,
    ctx: RequestContext,
    appeal: QualityAppeal,
    task: HumanReviewTask,
    resource: JsonResource,
) -> None:
    before = dict(resource.data)
    data = {
        **resource.data,
        "status": appeal.status,
        "reviewer_id": appeal.reviewer_id,
        "appeal_decision_id": appeal.appeal_decision_id,
        "decision": appeal.decision,
        "decision_reason": appeal.decision_reason,
        "withdrawal_reason": appeal.withdrawal_reason,
        "resource_version": appeal.resource_version,
        "claimed_at": _isoformat(appeal.claimed_at),
        "resolved_at": _isoformat(appeal.resolved_at),
        "withdrawn_at": _isoformat(appeal.withdrawn_at),
        "current_trace_id": ctx.trace_id,
        "trace_id": ctx.trace_id,
    }
    resource.data = data
    resource.status = appeal.status
    resource.trace_id = ctx.trace_id
    sync_label_review_projection(
        session,
        ctx,
        "human_review_tasks",
        appeal.review_task_id,
        data,
        status=appeal.status,
        trace_id=ctx.trace_id,
    )
    if task.status != appeal.status:
        task.status = appeal.status
    record_audit(
        session,
        ctx,
        action="quality_appeal.review_task.updated",
        object_type="human_review_task",
        object_id=appeal.review_task_id,
        before=before,
        after=data,
    )


def _create_appeal_decision(
    session: Session,
    ctx: RequestContext,
    appeal: QualityAppeal,
    *,
    decision: str,
    reason: str,
    decided_at: datetime,
) -> str:
    decision_id = f"hrd_qap_{uuid.uuid4().hex[:16]}"
    payload = {
        "id": decision_id,
        "decision_id": decision_id,
        "appeal_decision_id": decision_id,
        "decision_type": "quality_appeal",
        "queue": "quality_appeal",
        "appeal_id": appeal.appeal_id,
        "review_task_id": appeal.review_task_id,
        "decision": decision,
        "reason": reason,
        "decided_by": ctx.user_id,
        "decided_at": decided_at.isoformat(),
        "supersedes_source_decision_id": appeal.source_decision_id,
        "source_decision_id": appeal.source_decision_id,
        "source_result_sha256": appeal.source_result_sha256,
        "root_trace_id": appeal.root_trace_id,
        "source_trace_id": appeal.source_trace_id,
        "current_trace_id": ctx.trace_id,
        "trace_id": ctx.trace_id,
        "affected_objects": [
            {"type": "quality_appeal", "id": appeal.appeal_id},
            {"type": "human_review_decision", "id": appeal.source_decision_id},
        ],
    }
    session.add(
        HumanReviewDecision(
            decision_id=decision_id,
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            review_task_id=appeal.review_task_id,
            terminal_review_task_id=appeal.review_task_id,
            status="resolved",
            trace_id=ctx.trace_id,
            payload=payload,
        )
    )
    session.flush()
    upsert_resource(
        session,
        ctx,
        "human_review_decisions",
        decision_id,
        payload,
        status="resolved",
        trace_id=ctx.trace_id,
        audit_action="quality_appeal.decision.created",
    )
    enqueue_event(
        session,
        ctx,
        event_type="quality_appeal.decision.created",
        aggregate_type="human_review_decision",
        aggregate_id=decision_id,
        payload=payload,
    )
    session.flush()
    return decision_id


def _conditional_transition(
    session: Session,
    ctx: RequestContext,
    appeal: QualityAppeal,
    *,
    expected_resource_version: int,
    expected_status: str | tuple[str, ...],
    values: dict[str, Any],
    expected_reviewer_id: str | None = None,
) -> None:
    statuses = (expected_status,) if isinstance(expected_status, str) else expected_status
    conditions = [
        QualityAppeal.appeal_id == appeal.appeal_id,
        QualityAppeal.tenant_id == ctx.tenant_id,
        QualityAppeal.project_id == ctx.project_id,
        QualityAppeal.resource_version == expected_resource_version,
        QualityAppeal.status.in_(statuses),
    ]
    if expected_reviewer_id is not None:
        conditions.append(QualityAppeal.reviewer_id == expected_reviewer_id)
    result = session.execute(
        update(QualityAppeal)
        .where(*conditions)
        .values(
            **values,
            current_trace_id=ctx.trace_id,
            resource_version=expected_resource_version + 1,
        )
        .execution_options(synchronize_session=False)
    )
    if getattr(result, "rowcount", None) != 1:
        raise ApiError(
            "QUALITY_APPEAL_VERSION_CONFLICT",
            "申诉已被其他请求更新，请刷新后重试",
            409,
            retryable=True,
        )
    session.flush()
    session.refresh(appeal)


def _record_transition(
    session: Session,
    ctx: RequestContext,
    appeal: QualityAppeal,
    *,
    action: str,
    before: dict[str, Any],
) -> dict[str, Any]:
    after = quality_appeal_data(appeal)
    record_audit(
        session,
        ctx,
        action=action,
        object_type="quality_appeal",
        object_id=appeal.appeal_id,
        before=before,
        after=after,
    )
    enqueue_event(
        session,
        ctx,
        event_type=action,
        aggregate_type="quality_appeal",
        aggregate_id=appeal.appeal_id,
        payload=after,
    )
    return after


def claim_quality_appeal(
    session: Session,
    ctx: RequestContext,
    appeal_id: str,
    *,
    expected_resource_version: int,
) -> dict[str, Any]:
    appeal = get_quality_appeal(session, ctx, appeal_id, for_update=True)
    _assert_expected_version(appeal, expected_resource_version)
    _assert_reviewer_separation(appeal, ctx)
    if appeal.status != "submitted":
        raise ApiError(
            "QUALITY_APPEAL_INVALID_TRANSITION",
            "仅 submitted 状态的申诉可以领取",
            409,
        )
    task, task_resource = _appeal_review_task_for_update(session, ctx, appeal)
    before = quality_appeal_data(appeal)
    _conditional_transition(
        session,
        ctx,
        appeal,
        expected_resource_version=expected_resource_version,
        expected_status="submitted",
        values={
            "status": "under_review",
            "reviewer_id": ctx.user_id,
            "claimed_at": datetime.now(UTC),
        },
    )
    _update_appeal_review_task(session, ctx, appeal, task, task_resource)
    return _record_transition(
        session,
        ctx,
        appeal,
        action="quality_appeal.claimed",
        before=before,
    )


def decide_quality_appeal(
    session: Session,
    ctx: RequestContext,
    appeal_id: str,
    *,
    decision: str,
    reason: str,
    expected_resource_version: int,
) -> dict[str, Any]:
    appeal = get_quality_appeal(session, ctx, appeal_id, for_update=True)
    _assert_expected_version(appeal, expected_resource_version)
    _assert_reviewer_separation(appeal, ctx)
    if appeal.status != "under_review":
        raise ApiError(
            "QUALITY_APPEAL_INVALID_TRANSITION",
            "仅 under_review 状态的申诉可以结案",
            409,
        )
    if appeal.reviewer_id != ctx.user_id:
        raise ApiError(
            "QUALITY_APPEAL_REVIEWER_MISMATCH",
            "申诉已锁定给其他 reviewer",
            403,
        )
    task, task_resource = _appeal_review_task_for_update(session, ctx, appeal)
    before = quality_appeal_data(appeal)
    resolved_at = datetime.now(UTC)
    appeal_decision_id = _create_appeal_decision(
        session,
        ctx,
        appeal,
        decision=decision,
        reason=reason,
        decided_at=resolved_at,
    )
    _conditional_transition(
        session,
        ctx,
        appeal,
        expected_resource_version=expected_resource_version,
        expected_status="under_review",
        expected_reviewer_id=ctx.user_id,
        values={
            "status": "resolved",
            "decision": decision,
            "decision_reason": reason,
            "appeal_decision_id": appeal_decision_id,
            "resolved_at": resolved_at,
        },
    )
    _update_appeal_review_task(session, ctx, appeal, task, task_resource)
    return _record_transition(
        session,
        ctx,
        appeal,
        action="quality_appeal.resolved",
        before=before,
    )


def withdraw_quality_appeal(
    session: Session,
    ctx: RequestContext,
    appeal_id: str,
    *,
    reason: str,
    expected_resource_version: int,
) -> dict[str, Any]:
    appeal = get_quality_appeal(session, ctx, appeal_id, for_update=True)
    _assert_expected_version(appeal, expected_resource_version)
    if appeal.appellant_id != ctx.user_id:
        raise ApiError(
            "QUALITY_APPEAL_WITHDRAWAL_FORBIDDEN",
            "仅申诉申请人可以撤回申诉",
            403,
        )
    if appeal.status not in {"submitted", "under_review"}:
        raise ApiError(
            "QUALITY_APPEAL_INVALID_TRANSITION",
            "仅 submitted 或 under_review 状态的申诉可以撤回",
            409,
        )
    task, task_resource = _appeal_review_task_for_update(session, ctx, appeal)
    before = quality_appeal_data(appeal)
    _conditional_transition(
        session,
        ctx,
        appeal,
        expected_resource_version=expected_resource_version,
        expected_status=("submitted", "under_review"),
        values={
            "status": "withdrawn",
            "withdrawal_reason": reason,
            "withdrawn_at": datetime.now(UTC),
        },
    )
    _update_appeal_review_task(session, ctx, appeal, task, task_resource)
    return _record_transition(
        session,
        ctx,
        appeal,
        action="quality_appeal.withdrawn",
        before=before,
    )
