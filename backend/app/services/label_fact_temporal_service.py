from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.domain.label_mapping import sha256_document
from app.models import (
    HumanReviewDecision,
    LabelAggregate,
    LabelFact,
    LabelFactHead,
    LabelVersion,
    LabelVersionItem,
)
from app.schemas.label_facts import LabelFactAsOfRequest, LabelFactRevisionCreate
from app.services.audit_service import record_audit
from app.services.idempotency_service import replay_or_conflict, save_idempotency_result
from app.services.outbox_service import enqueue_event

APPEND_OPERATION = "label_facts.append_revision"
_AUTHORITY_RANK = {
    "l2-auto-accepted": 10,
    "human-confirmed": 100,
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    normalized = value
    if normalized.tzinfo is None or normalized.utcoffset() is None:
        normalized = normalized.replace(tzinfo=UTC)
    encoded = normalized.astimezone(UTC).isoformat()
    return encoded.replace("+00:00", "Z")


def _logical_key_document(
    ctx: RequestContext,
    request: LabelFactRevisionCreate,
) -> dict[str, str]:
    return {
        "assertion_slot": request.assertion_slot,
        "event_or_segment_id": request.event_or_segment_id,
        "fact_namespace": request.fact_namespace,
        "label_id": request.label_id,
        "project_id": ctx.project_id,
        "schema_version": "auris.label-fact-logical-key/1",
        "subject_key": request.subject_key,
        "subject_scope": request.subject_scope,
        "tenant_id": ctx.tenant_id,
    }


def label_fact_logical_key_sha256(
    ctx: RequestContext,
    request: LabelFactRevisionCreate,
) -> str:
    return sha256_document(_logical_key_document(ctx, request))


def _append_request_hash(
    ctx: RequestContext,
    request: LabelFactRevisionCreate,
    *,
    logical_key_sha256: str,
) -> str:
    return sha256_document(
        {
            "actor_kind": ctx.actor_kind,
            "body": request.model_dump(mode="json", exclude_none=False),
            "logical_key_sha256": logical_key_sha256,
            "operation": APPEND_OPERATION,
            "schema_version": "auris.label-fact-append-command/1",
            "user_id": ctx.user_id,
        }
    )


def _source_aggregate(
    session: Session,
    ctx: RequestContext,
    request: LabelFactRevisionCreate,
) -> LabelAggregate:
    aggregate = session.scalar(
        select(LabelAggregate)
        .where(
            LabelAggregate.tenant_id == ctx.tenant_id,
            LabelAggregate.project_id == ctx.project_id,
            LabelAggregate.aggregate_id == request.aggregate_id,
        )
        .with_for_update()
    )
    if aggregate is None:
        raise ApiError(
            "LABEL_FACT_SOURCE_NOT_FOUND",
            "当前租户项目中不存在指定的标签事实来源",
            404,
            details=[{"aggregate_id": request.aggregate_id}],
        )
    expected = {
        "label_id": request.label_id,
        "label_version_id": request.label_version_id,
        "subject_key": request.subject_key,
        "subject_scope": request.subject_scope,
        "value_type": request.value_type,
    }
    actual = {
        "label_id": aggregate.label_id,
        "label_version_id": aggregate.label_version_id,
        "subject_key": aggregate.subject_key,
        "subject_scope": aggregate.subject_scope,
        "value_type": aggregate.value_type,
    }
    # Aggregate revisions freeze the machine-produced value exactly. A human
    # decision may intentionally replace it; the decision's immutable
    # ``after_json`` is validated below as the authoritative value source.
    if request.source_kind == "aggregate":
        expected["value_sha256"] = sha256_document(request.value)
        actual["value_sha256"] = sha256_document(aggregate.value_json)
    if actual != expected:
        raise ApiError(
            "LABEL_FACT_SOURCE_MISMATCH",
            "LabelFact 请求与冻结 Aggregate 来源不一致",
            409,
            details=[{"actual": actual, "expected": expected}],
        )
    return aggregate


def _validate_human_source(
    session: Session,
    ctx: RequestContext,
    request: LabelFactRevisionCreate,
    aggregate: LabelAggregate,
) -> None:
    if request.source_kind != "human-decision":
        return
    decision = session.scalar(
        select(HumanReviewDecision).where(
            HumanReviewDecision.tenant_id == ctx.tenant_id,
            HumanReviewDecision.project_id == ctx.project_id,
            HumanReviewDecision.decision_id == request.human_review_decision_id,
        )
    )
    if decision is None:
        raise ApiError(
            "LABEL_FACT_HUMAN_DECISION_NOT_FOUND",
            "当前租户项目中不存在指定的人审决定",
            404,
        )
    normalized_decision = decision.payload.get("decision")
    if decision.status != "success" or normalized_decision not in {"accepted", "modified"}:
        raise ApiError(
            "LABEL_FACT_HUMAN_DECISION_INVALID",
            "只有已接受或已修改的人审终态可以产生权威事实",
            409,
            details=[
                {
                    "decision_id": decision.decision_id,
                    "decision": normalized_decision,
                    "status": decision.status,
                }
            ],
        )
    if decision.review_task_id != aggregate.review_task_id:
        raise ApiError(
            "LABEL_FACT_HUMAN_DECISION_SOURCE_MISMATCH",
            "人审决定与 Aggregate 的冻结复核任务不一致",
            409,
            details=[
                {
                    "aggregate_review_task_id": aggregate.review_task_id,
                    "decision_review_task_id": decision.review_task_id,
                }
            ],
        )
    affected_objects = decision.payload.get("affected_objects") or []
    affects_aggregate = any(
        isinstance(item, dict)
        and item.get("type") == "label_aggregate"
        and item.get("id") == aggregate.aggregate_id
        for item in affected_objects
    )
    after_targets = (decision.payload.get("after_json") or {}).get("targets") or {}
    target_snapshot = after_targets.get(f"label_aggregates:{aggregate.aggregate_id}")
    target_value_hash = (
        sha256_document(target_snapshot.get("value"))
        if isinstance(target_snapshot, dict) and "value" in target_snapshot
        else None
    )
    requested_value_hash = sha256_document(request.value)
    if not affects_aggregate or target_value_hash != requested_value_hash:
        raise ApiError(
            "LABEL_FACT_HUMAN_DECISION_SOURCE_MISMATCH",
            "LabelFact 值不属于该人审决定冻结的 Aggregate after_json",
            409,
            details=[
                {
                    "aggregate_id": aggregate.aggregate_id,
                    "decision_id": decision.decision_id,
                    "requested_value_sha256": requested_value_hash,
                    "target_value_sha256": target_value_hash,
                }
            ],
        )


def _validate_label_binding(
    session: Session,
    ctx: RequestContext,
    request: LabelFactRevisionCreate,
) -> None:
    version = session.scalar(
        select(LabelVersion).where(
            LabelVersion.tenant_id == ctx.tenant_id,
            LabelVersion.project_id == ctx.project_id,
            LabelVersion.label_version_id == request.label_version_id,
        )
    )
    item = session.scalar(
        select(LabelVersionItem).where(
            LabelVersionItem.tenant_id == ctx.tenant_id,
            LabelVersionItem.project_id == ctx.project_id,
            LabelVersionItem.label_version_id == request.label_version_id,
            LabelVersionItem.label_id == request.label_id,
        )
    )
    if version is None or item is None:
        raise ApiError(
            "LABEL_FACT_LABEL_BINDING_NOT_FOUND",
            "LabelFact 引用的不可变标签版本或标签项不存在",
            404,
        )
    if item.value_type != request.value_type:
        raise ApiError(
            "LABEL_FACT_VALUE_TYPE_MISMATCH",
            "LabelFact value_type 与冻结标签定义不一致",
            409,
            details=[
                {
                    "actual_value_type": request.value_type,
                    "expected_value_type": item.value_type,
                }
            ],
        )


def _current_head(
    session: Session,
    ctx: RequestContext,
    *,
    fact_namespace: str,
    logical_key_sha256: str,
) -> LabelFactHead | None:
    return session.scalar(
        select(LabelFactHead)
        .where(
            LabelFactHead.tenant_id == ctx.tenant_id,
            LabelFactHead.project_id == ctx.project_id,
            LabelFactHead.fact_namespace == fact_namespace,
            LabelFactHead.logical_key_sha == logical_key_sha256,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _validate_head(
    session: Session,
    ctx: RequestContext,
    head: LabelFactHead,
) -> LabelFact:
    current = session.scalar(
        select(LabelFact).where(
            LabelFact.tenant_id == ctx.tenant_id,
            LabelFact.project_id == ctx.project_id,
            LabelFact.fact_id == head.current_fact_id,
        )
    )
    expected_content_sha = head.payload.get("current_content_sha256")
    if (
        current is None
        or current.fact_namespace != head.fact_namespace
        or current.logical_key_sha != head.logical_key_sha
        or current.revision != head.current_revision
        or not isinstance(current.content_sha256, str)
        or expected_content_sha != current.content_sha256
    ):
        raise ApiError(
            "LABEL_FACT_HEAD_DRIFT",
            "LabelFact Head 与其当前不可变 revision 不一致",
            409,
            details=[
                {
                    "current_fact_id": head.current_fact_id,
                    "current_revision": head.current_revision,
                    "fact_head_id": head.fact_head_id,
                    "generation": head.generation,
                }
            ],
        )
    return current


def _fact_content_document(
    ctx: RequestContext,
    request: LabelFactRevisionCreate,
    *,
    logical_key_sha256: str,
    revision: int,
    recorded_at: datetime,
    supersedes_fact_id: str | None,
    root_trace_id: str,
) -> dict[str, Any]:
    return {
        "action_trace_id": ctx.trace_id,
        "aggregate_id": request.aggregate_id,
        "assertion_slot": request.assertion_slot,
        "authority": request.authority,
        "event_or_segment_id": request.event_or_segment_id,
        "fact_namespace": request.fact_namespace,
        "fact_set_id": request.fact_set_id,
        "human_review_decision_id": request.human_review_decision_id,
        "label_id": request.label_id,
        "label_version_id": request.label_version_id,
        "logical_key_sha256": logical_key_sha256,
        "occurred_at": _iso(request.occurred_at),
        "occurred_at_origin": request.occurred_at_origin,
        "project_id": ctx.project_id,
        "recorded_at": _iso(recorded_at),
        "revision": revision,
        "root_trace_id": root_trace_id,
        "schema_version": "auris.label-fact-revision/1",
        "source_kind": request.source_kind,
        "subject_key": request.subject_key,
        "subject_scope": request.subject_scope,
        "supersedes_fact_id": supersedes_fact_id,
        "tenant_id": ctx.tenant_id,
        "value": request.value,
        "value_type": request.value_type,
    }


def _fact_response(
    fact: LabelFact,
    head: LabelFactHead,
    *,
    audit_id: int,
    outbox_event_id: int,
) -> dict[str, Any]:
    if fact.recorded_at is None or fact.occurred_at is None or fact.revision is None:
        raise ApiError("LABEL_FACT_TEMPORAL_DRIFT", "LabelFact 缺少双时态字段", 409)
    return {
        "action_trace_id": fact.action_trace_id,
        "audit_id": audit_id,
        "content_sha256": fact.content_sha256,
        "fact_head_generation": head.generation,
        "fact_head_id": head.fact_head_id,
        "fact_id": fact.fact_id,
        "fact_namespace": fact.fact_namespace,
        "label_id": fact.label_id,
        "label_version_id": fact.label_version_id,
        "logical_key_sha256": fact.logical_key_sha,
        "occurred_at": _iso(fact.occurred_at),
        "outbox_event_id": outbox_event_id,
        "recorded_at": _iso(fact.recorded_at),
        "revision": fact.revision,
        "root_trace_id": fact.root_trace_id,
        "status": fact.status,
        "trace_id": fact.trace_id,
    }


def append_label_fact_revision(
    session: Session,
    ctx: RequestContext,
    request: LabelFactRevisionCreate,
) -> dict[str, Any]:
    """Append one authoritative revision and atomically advance its logical Head.

    During the 0035 Expand window the legacy ``status/active_slot`` projection is
    still maintained for old readers. As-of reads in this module ignore that
    mutable compatibility projection and resolve revisions by ``recorded_at``.
    """

    logical_key_sha256 = label_fact_logical_key_sha256(ctx, request)
    body_hash = _append_request_hash(
        ctx,
        request,
        logical_key_sha256=logical_key_sha256,
    )
    operation = f"{APPEND_OPERATION}:{logical_key_sha256}"
    replay = replay_or_conflict(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
    )
    if replay is not None:
        return replay

    aggregate = _source_aggregate(session, ctx, request)
    _validate_human_source(session, ctx, request, aggregate)
    _validate_label_binding(session, ctx, request)

    head = _current_head(
        session,
        ctx,
        fact_namespace=request.fact_namespace,
        logical_key_sha256=logical_key_sha256,
    )
    current: LabelFact | None = None
    if head is None:
        if request.expected_head_generation != 0:
            raise ApiError(
                "LABEL_FACT_HEAD_GENERATION_CONFLICT",
                "LabelFact Head 尚未创建，expected_head_generation 必须为 0",
                409,
                details=[
                    {
                        "actual_generation": 0,
                        "expected_generation": request.expected_head_generation,
                    }
                ],
            )
        orphan = session.scalar(
            select(LabelFact.fact_id).where(
                LabelFact.tenant_id == ctx.tenant_id,
                LabelFact.project_id == ctx.project_id,
                LabelFact.fact_namespace == request.fact_namespace,
                LabelFact.logical_key_sha == logical_key_sha256,
            )
        )
        if orphan is not None:
            raise ApiError(
                "LABEL_FACT_HEAD_DRIFT",
                "逻辑 key 已存在 temporal Fact 但缺少 Head",
                409,
                details=[{"orphan_fact_id": orphan}],
            )
        revision = 1
        generation = 1
    else:
        if request.expected_head_generation != head.generation:
            raise ApiError(
                "LABEL_FACT_HEAD_GENERATION_CONFLICT",
                "LabelFact Head generation 已变化",
                409,
                details=[
                    {
                        "actual_generation": head.generation,
                        "expected_generation": request.expected_head_generation,
                    }
                ],
            )
        current = _validate_head(session, ctx, head)
        current_rank = _AUTHORITY_RANK.get(current.authority)
        requested_rank = _AUTHORITY_RANK[request.authority]
        if current_rank is None:
            raise ApiError(
                "LABEL_FACT_AUTHORITY_UNKNOWN",
                "当前 LabelFact authority 无法安全比较",
                409,
            )
        if current_rank > requested_rank:
            raise ApiError(
                "LABEL_FACT_LOWER_AUTHORITY_REJECTED",
                "低权威来源不能覆盖当前 LabelFact Head",
                409,
                details=[
                    {
                        "current_authority": current.authority,
                        "current_fact_id": current.fact_id,
                        "requested_authority": request.authority,
                    }
                ],
            )
        revision = head.current_revision + 1
        generation = head.generation + 1

    recorded_at = _utcnow()
    root_trace_id = aggregate.trace_id
    supersedes_fact_id = current.fact_id if current is not None else None
    content_document = _fact_content_document(
        ctx,
        request,
        logical_key_sha256=logical_key_sha256,
        revision=revision,
        recorded_at=recorded_at,
        supersedes_fact_id=supersedes_fact_id,
        root_trace_id=root_trace_id,
    )
    content_sha256 = sha256_document(content_document)
    fact_id = (
        "lf_"
        + uuid.uuid5(
            uuid.NAMESPACE_URL,
            (
                f"auris-flow:label-fact:{ctx.tenant_id}:{ctx.project_id}:"
                f"{request.fact_namespace}:{logical_key_sha256}:{revision}:{content_sha256}"
            ),
        ).hex[:24]
    )

    # 0035 intentionally keeps the old single-active index. This is the only
    # compatibility mutation and is removed by the later Contract migration.
    if current is not None:
        current.status = "superseded"
        current.active_slot = None

    fact = LabelFact(
        fact_id=fact_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        aggregate_id=request.aggregate_id,
        supersedes_fact_id=supersedes_fact_id,
        fact_namespace=request.fact_namespace,
        logical_key_sha=logical_key_sha256,
        revision=revision,
        event_or_segment_id=request.event_or_segment_id,
        assertion_slot=request.assertion_slot,
        occurred_at=request.occurred_at,
        recorded_at=recorded_at,
        occurred_at_origin=request.occurred_at_origin,
        source_kind=request.source_kind,
        human_review_decision_id=request.human_review_decision_id,
        recompute_run_item_id=None,
        fact_set_id=request.fact_set_id,
        content_sha256=content_sha256,
        root_trace_id=root_trace_id,
        action_trace_id=ctx.trace_id,
        label_version_id=request.label_version_id,
        subject_scope=request.subject_scope,
        subject_key=request.subject_key,
        label_id=request.label_id,
        value_type=request.value_type,
        value_json=request.value,
        authority=request.authority,
        status="active",
        active_slot="active",
        review_decision_id=request.human_review_decision_id,
        trace_id=root_trace_id,
        payload={
            "action_trace_id": ctx.trace_id,
            "logical_key_document": _logical_key_document(ctx, request),
            "root_trace_id": root_trace_id,
            "source_aggregate_hash": aggregate.deterministic_hash,
        },
    )
    session.add(fact)
    session.flush()

    if head is None:
        head = LabelFactHead(
            fact_head_id=f"lfh_{logical_key_sha256[:24]}",
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            fact_namespace=request.fact_namespace,
            logical_key_sha=logical_key_sha256,
            current_fact_id=fact.fact_id,
            current_revision=revision,
            generation=generation,
            root_trace_id=root_trace_id,
            action_trace_id=ctx.trace_id,
            trace_id=root_trace_id,
            payload={
                "current_content_sha256": content_sha256,
                "previous_fact_id": None,
            },
        )
        session.add(head)
    else:
        head.current_fact_id = fact.fact_id
        head.current_revision = revision
        head.generation = generation
        head.action_trace_id = ctx.trace_id
        head.trace_id = root_trace_id
        head.payload = {
            **head.payload,
            "current_content_sha256": content_sha256,
            "previous_fact_id": supersedes_fact_id,
        }

    summary = {
        "action_trace_id": ctx.trace_id,
        "aggregate_id": request.aggregate_id,
        "authority": request.authority,
        "content_sha256": content_sha256,
        "event_or_segment_id": request.event_or_segment_id,
        "fact_id": fact.fact_id,
        "fact_namespace": request.fact_namespace,
        "human_review_decision_id": request.human_review_decision_id,
        "label_id": request.label_id,
        "label_version_id": request.label_version_id,
        "logical_key_sha256": logical_key_sha256,
        "occurred_at": _iso(request.occurred_at),
        "recorded_at": _iso(recorded_at),
        "resource_version": revision,
        "revision": revision,
        "root_trace_id": root_trace_id,
        "source_kind": request.source_kind,
        "supersedes_fact_id": supersedes_fact_id,
    }
    audit = record_audit(
        session,
        ctx,
        action="label_fact.created",
        object_type="label_fact",
        object_id=fact.fact_id,
        after=summary,
        trace_id=root_trace_id,
    )
    outbox_event = enqueue_event(
        session,
        ctx,
        event_type="label_fact.created",
        aggregate_type="label_fact",
        aggregate_id=fact.fact_id,
        payload=summary,
    )
    session.flush()
    response = _fact_response(
        fact,
        head,
        audit_id=audit.audit_id,
        outbox_event_id=outbox_event.event_id,
    )
    save_idempotency_result(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=201,
        response_json=response,
    )
    return response


def _fact_data(fact: LabelFact) -> dict[str, Any]:
    if (
        fact.fact_namespace is None
        or fact.logical_key_sha is None
        or fact.revision is None
        or fact.occurred_at is None
        or fact.recorded_at is None
        or fact.content_sha256 is None
    ):
        raise ApiError(
            "LABEL_FACT_TEMPORAL_DRIFT",
            "as-of 查询遇到未完成双时态回填的 LabelFact",
            409,
            details=[{"fact_id": fact.fact_id}],
        )
    return {
        "authority": fact.authority,
        "content_sha256": fact.content_sha256,
        "event_or_segment_id": fact.event_or_segment_id,
        "fact_id": fact.fact_id,
        "fact_namespace": fact.fact_namespace,
        "label_id": fact.label_id,
        "label_version_id": fact.label_version_id,
        "logical_key_sha256": fact.logical_key_sha,
        "occurred_at": _iso(fact.occurred_at),
        "recorded_at": _iso(fact.recorded_at),
        "revision": fact.revision,
        "source_kind": fact.source_kind,
        "subject_key": fact.subject_key,
        "subject_scope": fact.subject_scope,
        "supersedes_fact_id": fact.supersedes_fact_id,
        "value": fact.value_json,
        "value_type": fact.value_type,
    }


def list_label_facts_as_of(
    session: Session,
    ctx: RequestContext,
    request: LabelFactAsOfRequest,
) -> dict[str, Any]:
    """Resolve the latest revision known at ``fact_as_of`` for each logical key."""

    visible_versions = set(
        session.scalars(
            select(LabelVersion.label_version_id).where(
                LabelVersion.tenant_id == ctx.tenant_id,
                LabelVersion.project_id == ctx.project_id,
                LabelVersion.label_version_id.in_(request.label_version_ids),
            )
        )
    )
    missing_versions = sorted(set(request.label_version_ids) - visible_versions)
    if missing_versions:
        raise ApiError(
            "LABEL_FACT_LABEL_VERSION_NOT_FOUND",
            "as-of 查询包含不存在或不可访问的标签版本",
            404,
            details=[{"label_version_ids": missing_versions}],
        )

    statement = select(LabelFact).where(
        LabelFact.tenant_id == ctx.tenant_id,
        LabelFact.project_id == ctx.project_id,
        LabelFact.fact_namespace == request.fact_namespace,
        LabelFact.label_version_id.in_(request.label_version_ids),
        LabelFact.recorded_at.is_not(None),
        LabelFact.recorded_at <= request.fact_as_of,
        LabelFact.occurred_at.is_not(None),
        LabelFact.occurred_at >= request.occurred_from,
        LabelFact.occurred_at < request.occurred_to,
        LabelFact.logical_key_sha.is_not(None),
        LabelFact.revision.is_not(None),
    )
    if request.label_ids:
        statement = statement.where(LabelFact.label_id.in_(request.label_ids))
    rows = list(
        session.scalars(
            statement.order_by(
                LabelFact.logical_key_sha,
                LabelFact.recorded_at,
                LabelFact.revision,
                LabelFact.fact_id,
            )
        )
    )
    latest_by_key: dict[str, LabelFact] = {}
    for fact in rows:
        if fact.logical_key_sha is None or fact.revision is None or fact.recorded_at is None:
            raise ApiError("LABEL_FACT_TEMPORAL_DRIFT", "LabelFact 双时态字段不完整", 409)
        previous = latest_by_key.get(fact.logical_key_sha)
        if previous is None:
            latest_by_key[fact.logical_key_sha] = fact
            continue
        previous_order = (previous.recorded_at, previous.revision, previous.fact_id)
        candidate_order = (fact.recorded_at, fact.revision, fact.fact_id)
        if candidate_order > previous_order:
            latest_by_key[fact.logical_key_sha] = fact

    facts = [
        _fact_data(fact)
        for fact in sorted(
            latest_by_key.values(),
            key=lambda item: (
                item.label_version_id,
                item.label_id,
                item.logical_key_sha or "",
            ),
        )
    ]
    source_manifest = {
        "fact_as_of": _iso(request.fact_as_of),
        "fact_namespace": request.fact_namespace,
        "facts": [
            {
                "content_sha256": item["content_sha256"],
                "fact_id": item["fact_id"],
                "logical_key_sha256": item["logical_key_sha256"],
                "recorded_at": item["recorded_at"],
                "revision": item["revision"],
            }
            for item in facts
        ],
        "label_ids": sorted(request.label_ids),
        "label_version_ids": sorted(request.label_version_ids),
        "occurred_from": _iso(request.occurred_from),
        "occurred_to": _iso(request.occurred_to),
        "schema_version": "auris.label-fact-as-of-manifest/1",
    }
    return {
        "fact_as_of": _iso(request.fact_as_of),
        "fact_count": len(facts),
        "fact_namespace": request.fact_namespace,
        "facts": facts,
        "occurred_from": _iso(request.occurred_from),
        "occurred_to": _iso(request.occurred_to),
        "source_manifest_sha256": sha256_document(source_manifest),
    }
