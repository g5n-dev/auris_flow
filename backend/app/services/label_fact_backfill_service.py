from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, NoReturn

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.domain.label_mapping import sha256_document
from app.domain.label_mapping.canonical import CanonicalJsonError
from app.models import HumanReviewDecision, LabelAggregate, LabelFact, LabelFactHead
from app.services.audit_service import record_audit
from app.services.outbox_service import enqueue_event

FACT_NAMESPACE = "production"
ASSERTION_SLOT = "canonical"
BACKFILL_ACTION = "label_fact.temporal_backfilled"
_AUTO_AUTHORITY = "l2-auto-accepted"
_HUMAN_AUTHORITY = "human-confirmed"
_SUPPORTED_AUTHORITIES = frozenset({_AUTO_AUTHORITY, _HUMAN_AUTHORITY})


@dataclass(frozen=True)
class _FactProjection:
    fact: LabelFact
    logical_key_sha256: str
    revision: int
    occurred_at: datetime
    recorded_at: datetime
    source_kind: str
    aggregate_id: str | None
    human_review_decision_id: str | None
    root_trace_id: str
    action_trace_id: str
    content_sha256: str
    legacy: bool


@dataclass(frozen=True)
class _ChainProjection:
    logical_key_sha256: str
    facts: tuple[_FactProjection, ...]
    existing_head: LabelFactHead | None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _same_datetime(left: datetime | None, right: datetime) -> bool:
    return left is not None and _as_utc(left) == _as_utc(right)


def _raise_conflict(
    code: str,
    message: str,
    *,
    fact_id: str,
    reason_code: str,
) -> NoReturn:
    # Details deliberately contain only object references and stable reason
    # codes. Legacy free text, customer values, and review notes never cross
    # the service boundary through an error response.
    raise ApiError(
        code,
        message,
        409,
        details=[{"fact_id": fact_id, "reason_code": reason_code}],
    )


def _logical_key_document(ctx: RequestContext, fact: LabelFact) -> dict[str, str]:
    return {
        "assertion_slot": ASSERTION_SLOT,
        "event_or_segment_id": fact.subject_key,
        "fact_namespace": FACT_NAMESPACE,
        "label_id": fact.label_id,
        "project_id": ctx.project_id,
        "schema_version": "auris.label-fact-logical-key/1",
        "subject_key": fact.subject_key,
        "subject_scope": fact.subject_scope,
        "tenant_id": ctx.tenant_id,
    }


def _logical_key_sha256(ctx: RequestContext, fact: LabelFact) -> str:
    return sha256_document(_logical_key_document(ctx, fact))


def _content_document(
    ctx: RequestContext,
    fact: LabelFact,
    *,
    logical_key_sha256: str,
    revision: int,
    occurred_at: datetime,
    recorded_at: datetime,
    source_kind: str,
    aggregate_id: str | None,
    human_review_decision_id: str | None,
    root_trace_id: str,
    action_trace_id: str,
) -> dict[str, Any]:
    return {
        "action_trace_id": action_trace_id,
        "aggregate_id": aggregate_id,
        "assertion_slot": ASSERTION_SLOT,
        "authority": fact.authority,
        "event_or_segment_id": fact.subject_key,
        "fact_namespace": FACT_NAMESPACE,
        "fact_set_id": None,
        "human_review_decision_id": human_review_decision_id,
        "label_id": fact.label_id,
        "label_version_id": fact.label_version_id,
        "logical_key_sha256": logical_key_sha256,
        "occurred_at": _iso(occurred_at),
        "occurred_at_origin": "legacy-recorded-fallback",
        "project_id": ctx.project_id,
        "recorded_at": _iso(recorded_at),
        "revision": revision,
        "root_trace_id": root_trace_id,
        "schema_version": "auris.label-fact-revision/1",
        "source_kind": source_kind,
        "subject_key": fact.subject_key,
        "subject_scope": fact.subject_scope,
        "supersedes_fact_id": fact.supersedes_fact_id,
        "tenant_id": ctx.tenant_id,
        "value": fact.value_json,
        "value_type": fact.value_type,
    }


def _has_any_temporal_state(fact: LabelFact) -> bool:
    return any(
        value is not None
        for value in (
            fact.fact_namespace,
            fact.logical_key_sha,
            fact.revision,
            fact.event_or_segment_id,
            fact.assertion_slot,
            fact.occurred_at,
            fact.recorded_at,
            fact.occurred_at_origin,
            fact.source_kind,
            fact.human_review_decision_id,
            fact.recompute_run_item_id,
            fact.fact_set_id,
            fact.content_sha256,
            fact.root_trace_id,
            fact.action_trace_id,
        )
    )


def _has_complete_temporal_state(fact: LabelFact) -> bool:
    required = (
        fact.fact_namespace,
        fact.logical_key_sha,
        fact.revision,
        fact.event_or_segment_id,
        fact.assertion_slot,
        fact.occurred_at,
        fact.recorded_at,
        fact.occurred_at_origin,
        fact.source_kind,
        fact.content_sha256,
        fact.root_trace_id,
        fact.action_trace_id,
    )
    return all(value is not None for value in required)


def _source_aggregate(
    aggregates: dict[str, LabelAggregate],
    fact: LabelFact,
) -> LabelAggregate:
    reviewed_aggregate_id = fact.aggregate_id
    if reviewed_aggregate_id is None:
        payload_value = fact.payload.get("reviewed_aggregate_id") or fact.payload.get(
            "legacy_reviewed_aggregate_id"
        )
        reviewed_aggregate_id = str(payload_value) if payload_value else None
    if reviewed_aggregate_id is None:
        _raise_conflict(
            "LABEL_FACT_BACKFILL_SOURCE_CONFLICT",
            "旧 LabelFact 缺少冻结 Aggregate 来源标识",
            fact_id=fact.fact_id,
            reason_code="AGGREGATE_ID_MISSING",
        )
    aggregate = aggregates.get(reviewed_aggregate_id)
    if aggregate is None:
        _raise_conflict(
            "LABEL_FACT_BACKFILL_SOURCE_CONFLICT",
            "旧 LabelFact 缺少同租户项目的冻结 Aggregate 来源",
            fact_id=fact.fact_id,
            reason_code="AGGREGATE_NOT_FOUND",
        )
    assert aggregate is not None
    core_matches = (
        aggregate.label_version_id == fact.label_version_id
        and aggregate.subject_scope == fact.subject_scope
        and aggregate.subject_key == fact.subject_key
        and aggregate.label_id == fact.label_id
        and aggregate.value_type == fact.value_type
        and aggregate.trace_id == fact.trace_id
    )
    value_matches = fact.authority == _HUMAN_AUTHORITY or (
        sha256_document(aggregate.value_json) == sha256_document(fact.value_json)
    )
    if not core_matches or not value_matches:
        _raise_conflict(
            "LABEL_FACT_BACKFILL_SOURCE_CONFLICT",
            "旧 LabelFact 与同 scope 的冻结 Aggregate 来源不一致",
            fact_id=fact.fact_id,
            reason_code="AGGREGATE_BINDING_MISMATCH",
        )
    return aggregate


def _human_source(
    decisions: dict[str, HumanReviewDecision],
    fact: LabelFact,
    aggregate: LabelAggregate,
) -> str:
    decision_id = fact.review_decision_id
    if decision_id is None:
        _raise_conflict(
            "LABEL_FACT_BACKFILL_HUMAN_SOURCE_CONFLICT",
            "人审权威旧 Fact 缺少同 scope 的终态决定",
            fact_id=fact.fact_id,
            reason_code="HUMAN_DECISION_NOT_FOUND",
        )
    assert decision_id is not None
    decision = decisions.get(decision_id)
    if decision is None:
        _raise_conflict(
            "LABEL_FACT_BACKFILL_HUMAN_SOURCE_CONFLICT",
            "人审权威旧 Fact 缺少同 scope 的终态决定",
            fact_id=fact.fact_id,
            reason_code="HUMAN_DECISION_NOT_FOUND",
        )
    assert decision is not None
    normalized_decision = decision.payload.get("decision")
    if decision.status != "success" or normalized_decision not in {"accepted", "modified"}:
        _raise_conflict(
            "LABEL_FACT_BACKFILL_HUMAN_SOURCE_CONFLICT",
            "只有接受或修改的终态人审决定可以回填权威来源",
            fact_id=fact.fact_id,
            reason_code="HUMAN_DECISION_NOT_AUTHORITATIVE",
        )
    if decision.review_task_id != aggregate.review_task_id:
        _raise_conflict(
            "LABEL_FACT_BACKFILL_HUMAN_SOURCE_CONFLICT",
            "人审决定与旧 Fact 的冻结 Aggregate 来源不一致",
            fact_id=fact.fact_id,
            reason_code="HUMAN_DECISION_SOURCE_MISMATCH",
        )
    affected_objects = decision.payload.get("affected_objects") or []
    affects_aggregate = any(
        isinstance(item, dict)
        and item.get("type") == "label_aggregate"
        and item.get("id") == aggregate.aggregate_id
        for item in affected_objects
    )
    after_targets = (decision.payload.get("after_json") or {}).get("targets") or {}
    target = after_targets.get(f"label_aggregates:{aggregate.aggregate_id}")
    value_matches = (
        isinstance(target, dict)
        and "value" in target
        and sha256_document(target["value"]) == sha256_document(fact.value_json)
    )
    if not affects_aggregate or not value_matches:
        _raise_conflict(
            "LABEL_FACT_BACKFILL_HUMAN_SOURCE_CONFLICT",
            "人审决定与旧 Fact 的冻结结果不一致",
            fact_id=fact.fact_id,
            reason_code="HUMAN_DECISION_SOURCE_MISMATCH",
        )
    return decision_id


def _validate_chain(facts: list[LabelFact]) -> None:
    immutable_projection = all(
        fact.status == "recorded" and fact.active_slot is None for fact in facts
    )
    for index, fact in enumerate(facts):
        predecessor_id = facts[index - 1].fact_id if index else None
        is_current = index == len(facts) - 1
        if fact.supersedes_fact_id != predecessor_id:
            _raise_conflict(
                "LABEL_FACT_BACKFILL_CHAIN_CONFLICT",
                "旧 LabelFact supersedes 链不连续",
                fact_id=fact.fact_id,
                reason_code="NON_CONTIGUOUS_CHAIN",
            )
        projection_matches = immutable_projection or (
            fact.status == ("active" if is_current else "superseded")
            and fact.active_slot == ("active" if is_current else None)
        )
        if not projection_matches:
            _raise_conflict(
                "LABEL_FACT_BACKFILL_CHAIN_CONFLICT",
                "旧 LabelFact 活跃投影与 supersedes 链不一致",
                fact_id=fact.fact_id,
                reason_code="ACTIVE_PROJECTION_MISMATCH",
            )


def _validate_existing_projection(
    projection: _FactProjection,
) -> None:
    fact = projection.fact
    expected = (
        fact.fact_namespace == FACT_NAMESPACE
        and fact.logical_key_sha == projection.logical_key_sha256
        and fact.revision == projection.revision
        and fact.event_or_segment_id == fact.subject_key
        and fact.assertion_slot == ASSERTION_SLOT
        and _same_datetime(fact.occurred_at, projection.occurred_at)
        and _same_datetime(fact.recorded_at, projection.recorded_at)
        and fact.occurred_at_origin == "legacy-recorded-fallback"
        and fact.source_kind == projection.source_kind
        and fact.aggregate_id == projection.aggregate_id
        and fact.human_review_decision_id == projection.human_review_decision_id
        and fact.recompute_run_item_id is None
        and fact.fact_set_id is None
        and fact.root_trace_id == projection.root_trace_id
        and fact.action_trace_id == projection.action_trace_id
        and fact.content_sha256 == projection.content_sha256
    )
    if not expected:
        _raise_conflict(
            "LABEL_FACT_BACKFILL_DRIFT",
            "旧 LabelFact 已存在的双时态投影与规范值不一致",
            fact_id=fact.fact_id,
            reason_code="TEMPORAL_PROJECTION_DRIFT",
        )


def _validate_existing_head(chain: _ChainProjection) -> None:
    head = chain.existing_head
    current = chain.facts[-1]
    if head is None:
        if any(not projection.legacy for projection in chain.facts):
            _raise_conflict(
                "LABEL_FACT_BACKFILL_DRIFT",
                "已回填的 LabelFact 链缺少原子 Head",
                fact_id=current.fact.fact_id,
                reason_code="TEMPORAL_HEAD_MISSING",
            )
        return
    expected = (
        not any(projection.legacy for projection in chain.facts)
        and head.fact_head_id == f"lfh_{chain.logical_key_sha256[:24]}"
        and head.fact_namespace == FACT_NAMESPACE
        and head.logical_key_sha == chain.logical_key_sha256
        and head.current_fact_id == current.fact.fact_id
        and head.current_revision == current.revision
        and head.generation == current.revision
        and head.root_trace_id == current.root_trace_id
        and head.action_trace_id == current.action_trace_id
        and head.trace_id == current.root_trace_id
        and head.payload.get("current_content_sha256") == current.content_sha256
        and head.payload.get("previous_fact_id") == current.fact.supersedes_fact_id
    )
    if not expected:
        _raise_conflict(
            "LABEL_FACT_BACKFILL_DRIFT",
            "LabelFact Head 与规范 revision 链不一致",
            fact_id=current.fact.fact_id,
            reason_code="TEMPORAL_HEAD_DRIFT",
        )


def _project_chains(
    session: Session,
    ctx: RequestContext,
    facts: list[LabelFact],
) -> list[_ChainProjection]:
    aggregate_ids = {
        str(
            fact.aggregate_id
            or fact.payload.get("reviewed_aggregate_id")
            or fact.payload.get("legacy_reviewed_aggregate_id")
        )
        for fact in facts
        if fact.aggregate_id
        or fact.payload.get("reviewed_aggregate_id")
        or fact.payload.get("legacy_reviewed_aggregate_id")
    }
    aggregates = {
        aggregate.aggregate_id: aggregate
        for aggregate in session.scalars(
            select(LabelAggregate).where(
                LabelAggregate.tenant_id == ctx.tenant_id,
                LabelAggregate.project_id == ctx.project_id,
                LabelAggregate.aggregate_id.in_(aggregate_ids),
            )
        )
    }
    decision_ids = {
        fact.review_decision_id
        for fact in facts
        if fact.authority == _HUMAN_AUTHORITY and fact.review_decision_id is not None
    }
    decisions = {
        decision.decision_id: decision
        for decision in session.scalars(
            select(HumanReviewDecision).where(
                HumanReviewDecision.tenant_id == ctx.tenant_id,
                HumanReviewDecision.project_id == ctx.project_id,
                HumanReviewDecision.decision_id.in_(decision_ids),
            )
        )
    }

    grouped: dict[str, list[LabelFact]] = defaultdict(list)
    for fact in facts:
        if fact.created_at is None:
            _raise_conflict(
                "LABEL_FACT_BACKFILL_DRIFT",
                "旧 LabelFact 缺少平台记录时间",
                fact_id=fact.fact_id,
                reason_code="CREATED_AT_MISSING",
            )
        if fact.authority not in _SUPPORTED_AUTHORITIES:
            _raise_conflict(
                "LABEL_FACT_BACKFILL_AUTHORITY_UNKNOWN",
                "旧 LabelFact authority 无法安全映射",
                fact_id=fact.fact_id,
                reason_code="UNKNOWN_AUTHORITY",
            )
        has_any = _has_any_temporal_state(fact)
        has_complete = _has_complete_temporal_state(fact)
        if has_any != has_complete:
            _raise_conflict(
                "LABEL_FACT_BACKFILL_DRIFT",
                "旧 LabelFact 存在局部双时态字段",
                fact_id=fact.fact_id,
                reason_code="PARTIAL_TEMPORAL_STATE",
            )
        grouped[_logical_key_sha256(ctx, fact)].append(fact)

    heads = list(
        session.scalars(
            select(LabelFactHead)
            .where(
                LabelFactHead.tenant_id == ctx.tenant_id,
                LabelFactHead.project_id == ctx.project_id,
                LabelFactHead.fact_namespace == FACT_NAMESPACE,
            )
            .with_for_update()
        )
    )
    head_by_key = {head.logical_key_sha: head for head in heads}
    unexpected_head = next((head for head in heads if head.logical_key_sha not in grouped), None)
    if unexpected_head is not None:
        _raise_conflict(
            "LABEL_FACT_BACKFILL_DRIFT",
            "生产 Fact Head 缺少同 scope 的逻辑事实链",
            fact_id=unexpected_head.current_fact_id,
            reason_code="TEMPORAL_HEAD_ORPHANED",
        )

    result: list[_ChainProjection] = []
    for logical_key_sha256 in sorted(grouped):
        chain_facts = sorted(
            grouped[logical_key_sha256],
            key=lambda item: (_as_utc(item.created_at), item.fact_id),
        )
        _validate_chain(chain_facts)
        projections: list[_FactProjection] = []
        for revision, fact in enumerate(chain_facts, start=1):
            aggregate = _source_aggregate(aggregates, fact)
            source_kind = "aggregate"
            human_review_decision_id: str | None = None
            if fact.authority == _HUMAN_AUTHORITY:
                source_kind = "human-decision"
                human_review_decision_id = _human_source(decisions, fact, aggregate)
            projected_aggregate_id = aggregate.aggregate_id if source_kind == "aggregate" else None

            legacy = not _has_any_temporal_state(fact)
            occurred_at = _as_utc(fact.created_at)
            recorded_at = occurred_at
            root_trace_id = fact.root_trace_id if not legacy else fact.trace_id
            action_trace_id = fact.action_trace_id if not legacy else ctx.trace_id
            if not root_trace_id or not action_trace_id:
                _raise_conflict(
                    "LABEL_FACT_BACKFILL_DRIFT",
                    "旧 LabelFact 缺少可用 Trace 绑定",
                    fact_id=fact.fact_id,
                    reason_code="TRACE_BINDING_MISSING",
                )
            try:
                content_sha256 = sha256_document(
                    _content_document(
                        ctx,
                        fact,
                        logical_key_sha256=logical_key_sha256,
                        revision=revision,
                        occurred_at=occurred_at,
                        recorded_at=recorded_at,
                        source_kind=source_kind,
                        aggregate_id=projected_aggregate_id,
                        human_review_decision_id=human_review_decision_id,
                        root_trace_id=root_trace_id,
                        action_trace_id=action_trace_id,
                    )
                )
            except CanonicalJsonError:
                _raise_conflict(
                    "LABEL_FACT_BACKFILL_SOURCE_CONFLICT",
                    "旧 LabelFact 内容不是有限规范 JSON",
                    fact_id=fact.fact_id,
                    reason_code="CONTENT_NOT_CANONICAL_JSON",
                )
            projection = _FactProjection(
                fact=fact,
                logical_key_sha256=logical_key_sha256,
                revision=revision,
                occurred_at=occurred_at,
                recorded_at=recorded_at,
                source_kind=source_kind,
                aggregate_id=projected_aggregate_id,
                human_review_decision_id=human_review_decision_id,
                root_trace_id=root_trace_id,
                action_trace_id=action_trace_id,
                content_sha256=content_sha256,
                legacy=legacy,
            )
            if not legacy:
                _validate_existing_projection(projection)
            projections.append(projection)
        chain = _ChainProjection(
            logical_key_sha256=logical_key_sha256,
            facts=tuple(projections),
            existing_head=head_by_key.get(logical_key_sha256),
        )
        _validate_existing_head(chain)
        result.append(chain)
    return result


def _apply_fact_projection(projection: _FactProjection) -> None:
    fact = projection.fact
    fact.fact_namespace = FACT_NAMESPACE
    fact.logical_key_sha = projection.logical_key_sha256
    fact.revision = projection.revision
    fact.event_or_segment_id = fact.subject_key
    fact.assertion_slot = ASSERTION_SLOT
    fact.occurred_at = projection.occurred_at
    fact.recorded_at = projection.recorded_at
    fact.occurred_at_origin = "legacy-recorded-fallback"
    fact.source_kind = projection.source_kind
    if projection.source_kind == "human-decision" and fact.aggregate_id is not None:
        fact.payload = {**fact.payload, "reviewed_aggregate_id": fact.aggregate_id}
    fact.aggregate_id = projection.aggregate_id
    fact.human_review_decision_id = projection.human_review_decision_id
    fact.recompute_run_item_id = None
    fact.fact_set_id = None
    fact.content_sha256 = projection.content_sha256
    fact.root_trace_id = projection.root_trace_id
    fact.action_trace_id = projection.action_trace_id


def backfill_legacy_label_facts(
    session: Session,
    ctx: RequestContext,
) -> dict[str, Any]:
    """Backfill one tenant/project's legacy facts into immutable temporal chains.

    The function performs a complete read/validation pass before changing any
    row. The caller owns the surrounding transaction; facts, Heads, Audit, and
    Outbox therefore commit or roll back together.
    """

    facts = list(
        session.scalars(
            select(LabelFact)
            .where(
                LabelFact.tenant_id == ctx.tenant_id,
                LabelFact.project_id == ctx.project_id,
                or_(
                    LabelFact.fact_namespace.is_(None),
                    LabelFact.fact_namespace == FACT_NAMESPACE,
                ),
            )
            .order_by(LabelFact.created_at, LabelFact.fact_id)
            .with_for_update()
        )
    )
    chains = _project_chains(session, ctx, facts)
    updated_count = sum(1 for chain in chains for projection in chain.facts if projection.legacy)
    created_head_count = sum(1 for chain in chains if chain.existing_head is None)
    result: dict[str, Any] = {
        "status": "success",
        "fact_namespace": FACT_NAMESPACE,
        "scanned_count": len(facts),
        "updated_count": updated_count,
        "created_head_count": created_head_count,
        "conflict_count": 0,
        "trace_id": ctx.trace_id,
        "audit_id": None,
        "outbox_event_id": None,
    }
    if updated_count == 0 and created_head_count == 0:
        return result

    for chain in chains:
        for projection in chain.facts:
            if projection.legacy:
                _apply_fact_projection(projection)
    # Composite Head FKs require the revision projection to exist first.
    session.flush()

    for chain in chains:
        if chain.existing_head is not None:
            continue
        current = chain.facts[-1]
        session.add(
            LabelFactHead(
                fact_head_id=f"lfh_{chain.logical_key_sha256[:24]}",
                tenant_id=ctx.tenant_id,
                project_id=ctx.project_id,
                fact_namespace=FACT_NAMESPACE,
                logical_key_sha=chain.logical_key_sha256,
                current_fact_id=current.fact.fact_id,
                current_revision=current.revision,
                generation=current.revision,
                root_trace_id=current.root_trace_id,
                action_trace_id=ctx.trace_id,
                trace_id=current.root_trace_id,
                payload={
                    "current_content_sha256": current.content_sha256,
                    "previous_fact_id": current.fact.supersedes_fact_id,
                },
            )
        )
    session.flush()

    summary_sha256 = sha256_document(
        {
            "created_head_count": created_head_count,
            "fact_namespace": FACT_NAMESPACE,
            "logical_key_sha256": sorted(chain.logical_key_sha256 for chain in chains),
            "schema_version": "auris.label-fact-temporal-backfill-summary/1",
            "scanned_count": len(facts),
            "tenant_id": ctx.tenant_id,
            "project_id": ctx.project_id,
            "updated_count": updated_count,
        }
    )
    summary = {
        "created_head_count": created_head_count,
        "fact_namespace": FACT_NAMESPACE,
        "resource_version": len(facts),
        "scanned_count": len(facts),
        "summary_sha256": summary_sha256,
        "updated_count": updated_count,
    }
    audit = record_audit(
        session,
        ctx,
        action=BACKFILL_ACTION,
        object_type="label_fact_backfill",
        object_id=FACT_NAMESPACE,
        after=summary,
    )
    event = enqueue_event(
        session,
        ctx,
        event_type=BACKFILL_ACTION,
        aggregate_type="label_fact_backfill",
        aggregate_id=FACT_NAMESPACE,
        payload=summary,
    )
    session.flush()
    result["audit_id"] = audit.audit_id
    result["outbox_event_id"] = event.event_id
    return result
