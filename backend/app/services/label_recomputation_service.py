from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, cast

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.core.rbac import require_any_role
from app.core.request_identifiers import public_id_from_hex
from app.models import (
    AuditLog,
    LabelAggregate,
    LabelAggregateMember,
    LabelAggregationRun,
    LabelFact,
    LabelFactSet,
    LabelFactSetHead,
    LabelMappingBundle,
    LabelObservation,
    LabelRecomputeRun,
    LabelRecomputeRunItem,
    LabelVersion,
    LabelVersionItem,
    OutboxEvent,
    RunCompletionReceipt,
    RunRecord,
)
from app.schemas.label_recomputations import (
    LabelRecomputeFactCandidate,
    LabelRecomputeMutationResponse,
    LabelRecomputeRunCreateRequest,
    LabelRecomputeRunItemCompletionRequest,
    LabelRecomputeRunItemMutationResponse,
    LabelRecomputeRunItemRetryRequest,
)
from app.services.audit_service import record_audit
from app.services.idempotency_service import (
    replay_or_conflict,
    require_idempotency,
    save_idempotency_result,
)
from app.services.label_fact_set_service import strict_canonical_sha256
from app.services.outbox_service import enqueue_event

CREATE_OPERATION = "label-recompute-runs.create"
COMPLETE_OPERATION = "label-recompute-run-items.complete"
RETRY_OPERATION = "label-recompute-run-items.retry"
_WRITER_ROLES = ("project_admin", "model_engineer")
_WORKER_ROLES = ("system", "model_engineer")


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    normalized = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return normalized.isoformat()


def _id(prefix: str, document: Any) -> str:
    return public_id_from_hex(
        prefix,
        strict_canonical_sha256(document),
        suffix_length=24,
    )


def _request_hash(
    ctx: RequestContext,
    *,
    operation: str,
    resource_id: str | None,
    body: dict[str, Any],
) -> str:
    return strict_canonical_sha256(
        {
            "actor_kind": ctx.actor_kind,
            "body": body,
            "operation": operation,
            "resource_id": resource_id,
            "user_id": ctx.user_id,
        }
    )


def _replay(
    session: Session,
    ctx: RequestContext,
    *,
    operation: str,
    body_hash: str,
) -> dict[str, Any] | None:
    require_idempotency(ctx)
    return replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)


def _save(
    session: Session,
    ctx: RequestContext,
    *,
    operation: str,
    body_hash: str,
    status_code: int,
    response: dict[str, Any],
) -> dict[str, Any]:
    save_idempotency_result(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=status_code,
        response_json=response,
    )
    return response


def _load_run(
    session: Session,
    ctx: RequestContext,
    run_id: str,
    *,
    for_update: bool,
) -> LabelRecomputeRun:
    statement = select(LabelRecomputeRun).where(
        LabelRecomputeRun.tenant_id == ctx.tenant_id,
        LabelRecomputeRun.project_id == ctx.project_id,
        LabelRecomputeRun.recompute_run_id == run_id,
    )
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    run = session.scalar(statement)
    if run is None:
        raise ApiError(
            "LABEL_RECOMPUTE_RUN_NOT_FOUND",
            "重算运行不存在于当前租户项目范围",
            404,
        )
    return run


def _load_item(
    session: Session,
    ctx: RequestContext,
    run_id: str,
    item_id: str,
    *,
    for_update: bool,
) -> LabelRecomputeRunItem:
    statement = select(LabelRecomputeRunItem).where(
        LabelRecomputeRunItem.tenant_id == ctx.tenant_id,
        LabelRecomputeRunItem.project_id == ctx.project_id,
        LabelRecomputeRunItem.recompute_run_id == run_id,
        LabelRecomputeRunItem.recompute_run_item_id == item_id,
    )
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    item = session.scalar(statement)
    if item is None:
        raise ApiError(
            "LABEL_RECOMPUTE_RUN_ITEM_NOT_FOUND",
            "重算分区不存在于当前租户项目运行范围",
            404,
        )
    return item


def _items(
    session: Session,
    run: LabelRecomputeRun,
    *,
    for_update: bool,
) -> list[LabelRecomputeRunItem]:
    statement = (
        select(LabelRecomputeRunItem)
        .where(
            LabelRecomputeRunItem.tenant_id == run.tenant_id,
            LabelRecomputeRunItem.project_id == run.project_id,
            LabelRecomputeRunItem.recompute_run_id == run.recompute_run_id,
        )
        .order_by(LabelRecomputeRunItem.partition_id)
    )
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return list(session.scalars(statement))


def _target_anchor(session: Session, ctx: RequestContext, version_id: str) -> LabelVersion:
    version = session.scalar(
        select(LabelVersion)
        .where(
            LabelVersion.tenant_id == ctx.tenant_id,
            LabelVersion.project_id == ctx.project_id,
            LabelVersion.label_version_id == version_id,
        )
        .with_for_update()
    )
    if (
        version is None
        or version.artifact_status not in {"approved", "published"}
        or version.resource_version <= 0
        or not isinstance(version.content_sha256, str)
        or len(version.content_sha256) != 64
    ):
        raise ApiError(
            "LABEL_RECOMPUTE_TARGET_ANCHOR_INVALID",
            "目标标签版本不存在或未形成可冻结强锚点",
            409,
        )
    return version


def _source_anchor(
    session: Session,
    ctx: RequestContext,
    request: LabelRecomputeRunCreateRequest,
) -> tuple[LabelFactSetHead, LabelFactSet]:
    head = session.scalar(
        select(LabelFactSetHead)
        .where(
            LabelFactSetHead.tenant_id == ctx.tenant_id,
            LabelFactSetHead.project_id == ctx.project_id,
            LabelFactSetHead.environment == request.source_environment,
            LabelFactSetHead.fact_namespace == request.source_fact_namespace,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if head is None:
        raise ApiError("LABEL_RECOMPUTE_SOURCE_HEAD_NOT_FOUND", "源 FactSet Head 不存在", 404)
    expected = {
        "generation": request.source_head_generation,
        "fact_set_id": request.source_fact_set_id,
        "manifest_sha256": request.source_manifest_sha256,
    }
    actual = {
        "generation": head.generation,
        "fact_set_id": head.current_fact_set_id,
        "manifest_sha256": head.current_manifest_sha256,
    }
    if actual != expected:
        raise ApiError(
            "LABEL_RECOMPUTE_SOURCE_HEAD_CONFLICT",
            "源 FactSet Head generation 或 manifest 已变化",
            409,
            details=[{"actual": actual, "expected": expected}],
        )
    fact_set = session.scalar(
        select(LabelFactSet).where(
            LabelFactSet.tenant_id == ctx.tenant_id,
            LabelFactSet.project_id == ctx.project_id,
            LabelFactSet.fact_set_id == request.source_fact_set_id,
            LabelFactSet.manifest_sha256 == request.source_manifest_sha256,
        )
    )
    if fact_set is None or fact_set.status != "published":
        raise ApiError(
            "LABEL_RECOMPUTE_SOURCE_FACT_SET_INVALID",
            "源 FactSet 必须是当前 Head 指向的已发布不可变 manifest",
            409,
        )
    return head, fact_set


def _mapping_anchor(
    session: Session,
    ctx: RequestContext,
    request: LabelRecomputeRunCreateRequest,
) -> LabelMappingBundle | None:
    if request.mapping_bundle_id is None:
        return None
    bundle = session.scalar(
        select(LabelMappingBundle).where(
            LabelMappingBundle.tenant_id == ctx.tenant_id,
            LabelMappingBundle.project_id == ctx.project_id,
            LabelMappingBundle.mapping_bundle_id == request.mapping_bundle_id,
            LabelMappingBundle.canonical_manifest_sha256 == request.mapping_bundle_sha256,
        )
    )
    if (
        bundle is None
        or bundle.status not in {"approved", "published"}
        or bundle.target_label_version_id != request.target_label_version_id
    ):
        raise ApiError(
            "LABEL_RECOMPUTE_MAPPING_ANCHOR_INVALID",
            "mapping bundle 不存在、未批准或目标版本不匹配",
            409,
        )
    return bundle


def _reservation_manifest(
    ctx: RequestContext,
    request: LabelRecomputeRunCreateRequest,
    target: LabelVersion,
) -> dict[str, Any]:
    return {
        "fact_as_of": _iso(request.fact_as_of),
        "fact_namespace": request.fact_namespace,
        "partition_manifest": {
            "partitions": [
                {"partition_id": partition.partition_id, "status": "pending"}
                for partition in request.partitions
            ],
            "schema_version": "auris.label-recompute-partitions/1",
        },
        "project_id": ctx.project_id,
        "schema_version": "auris.label-fact-set-reservation/1",
        "source_manifest_sha256": request.source_manifest_sha256,
        "target_label_version": {
            "artifact_status": target.artifact_status,
            "content_sha256": target.content_sha256,
            "label_version_id": target.label_version_id,
            "resource_version": target.resource_version,
        },
        "tenant_id": ctx.tenant_id,
    }


def _execution_run(
    ctx: RequestContext,
    *,
    recompute_run_id: str,
    item_id: str,
    partition_id: str,
    attempt_generation: int,
    request_sha256: str,
) -> RunRecord:
    execution_run_id = _id(
        "run",
        {
            "attempt_generation": attempt_generation,
            "item_id": item_id,
            "request_sha256": request_sha256,
        },
    )
    external_id = _id("dag", {"execution_run_id": execution_run_id})
    return RunRecord(
        run_id=execution_run_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        run_type="label_recompute_partition",
        status="submitted",
        run_key=f"{item_id}:{attempt_generation}",
        partition_key=partition_id,
        trace_id=ctx.trace_id,
        payload={
            "business_completion_required": True,
            "dispatch": {
                "adapter": "dagster",
                "details": {"dagster_run_id": external_id},
            },
            "label_recompute": {
                "attempt_generation": attempt_generation,
                "recompute_run_id": recompute_run_id,
                "recompute_run_item_id": item_id,
                "request_sha256": request_sha256,
            },
            "status": "submitted",
        },
    )


def _run_counts(items: list[LabelRecomputeRunItem]) -> tuple[int, int, int, int]:
    completed = sum(item.status == "succeeded" for item in items)
    failed = sum(item.status == "failed" for item in items)
    rows = sum(item.row_count for item in items if item.status == "succeeded")
    return len(items), completed, failed, rows


def _run_response(
    run: LabelRecomputeRun,
    items: list[LabelRecomputeRunItem],
    fact_set: LabelFactSet,
    ctx: RequestContext,
    *,
    audit_id: int,
    outbox_event_id: int,
) -> dict[str, Any]:
    total, completed, failed, rows = _run_counts(items)
    manifest = fact_set.manifest_sha256 if run.status == "candidate-complete" else None
    return LabelRecomputeMutationResponse(
        recompute_run_id=run.recompute_run_id,
        status=cast(Any, run.status),
        candidate_fact_set_id=run.candidate_fact_set_id,
        candidate_manifest_sha256=manifest,
        fact_namespace=run.fact_namespace,
        partition_count=total,
        completed_partition_count=completed,
        failed_partition_count=failed,
        row_count=rows,
        audit_id=audit_id,
        outbox_event_id=outbox_event_id,
        trace_id=ctx.trace_id,
    ).model_dump(mode="json")


def _item_response(
    run: LabelRecomputeRun,
    item: LabelRecomputeRunItem,
    fact_set: LabelFactSet,
    ctx: RequestContext,
    *,
    audit_id: int,
    outbox_event_id: int,
) -> dict[str, Any]:
    return LabelRecomputeRunItemMutationResponse(
        recompute_run_id=run.recompute_run_id,
        recompute_run_item_id=item.recompute_run_item_id,
        partition_id=item.partition_id,
        status=cast(Any, item.status),
        attempt_generation=item.attempt_generation,
        row_count=item.row_count,
        source_manifest_sha256=item.source_manifest_sha256,
        result_manifest_sha256=item.result_manifest_sha256,
        content_sha256=item.content_sha256,
        run_status=cast(Any, run.status),
        candidate_manifest_sha256=(
            fact_set.manifest_sha256 if run.status == "candidate-complete" else None
        ),
        audit_id=audit_id,
        outbox_event_id=outbox_event_id,
        trace_id=ctx.trace_id,
    ).model_dump(mode="json")


def _record_change(
    session: Session,
    ctx: RequestContext,
    *,
    action: str,
    object_type: str,
    object_id: str,
    before: dict[str, Any] | None,
    after: dict[str, Any],
) -> tuple[AuditLog, OutboxEvent]:
    audit = record_audit(
        session,
        ctx,
        action=action,
        object_type=object_type,
        object_id=object_id,
        before=before,
        after=after,
    )
    outbox = enqueue_event(
        session,
        ctx,
        event_type=action,
        aggregate_type=object_type,
        aggregate_id=object_id,
        payload=after,
    )
    session.flush()
    return audit, outbox


def create_label_recompute_run(
    session: Session,
    ctx: RequestContext,
    request: LabelRecomputeRunCreateRequest,
) -> dict[str, Any]:
    require_any_role(ctx, _WRITER_ROLES, action="label-recompute-runs.create")
    body = request.model_dump(mode="json")
    body_hash = _request_hash(ctx, operation=CREATE_OPERATION, resource_id=None, body=body)
    replay = _replay(session, ctx, operation=CREATE_OPERATION, body_hash=body_hash)
    if replay is not None:
        return replay

    target = _target_anchor(session, ctx, request.target_label_version_id)
    source_head, _source_fact_set = _source_anchor(session, ctx, request)
    mapping = _mapping_anchor(session, ctx, request)
    request_document = {
        **body,
        "project_id": ctx.project_id,
        "schema_version": "auris.label-recompute-run/1",
        "source_fact_set_head_id": source_head.fact_set_head_id,
        "target_content_sha256": target.content_sha256,
        "target_resource_version": target.resource_version,
        "tenant_id": ctx.tenant_id,
    }
    request_sha256 = strict_canonical_sha256(request_document)
    run_id = _id("lrr", request_document)
    fact_set_id = _id("lfs", {"recompute_run_id": run_id, "request_sha256": request_sha256})
    reservation = _reservation_manifest(ctx, request, target)
    partition_manifest = cast(dict[str, Any], reservation["partition_manifest"])
    partition_sha = strict_canonical_sha256(partition_manifest)
    result_sha = strict_canonical_sha256([])
    reservation_manifest = {
        "fact_as_of": _iso(request.fact_as_of),
        "fact_namespace": request.fact_namespace,
        "partition_manifest": partition_manifest,
        "partition_manifest_sha256": partition_sha,
        "project_id": ctx.project_id,
        "result_manifest_sha256": result_sha,
        "row_count": 0,
        "schema_version": "auris.label-fact-set-manifest/1",
        "source_manifest_sha256": request.source_manifest_sha256,
        "target_label_version": reservation["target_label_version"],
        "tenant_id": ctx.tenant_id,
    }
    fact_set = LabelFactSet(
        fact_set_id=fact_set_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        fact_namespace=request.fact_namespace,
        target_label_version_id=request.target_label_version_id,
        status="candidate",
        fact_as_of=request.fact_as_of,
        partition_manifest=partition_manifest,
        partition_manifest_sha256=partition_sha,
        source_manifest_sha256=request.source_manifest_sha256,
        result_manifest_sha256=result_sha,
        row_count=0,
        manifest_sha256=strict_canonical_sha256(reservation_manifest),
        root_trace_id=ctx.trace_id,
        action_trace_id=ctx.trace_id,
        trace_id=ctx.trace_id,
        payload={
            "frozen_manifest": reservation_manifest,
            "materialization_status": "reserved",
            "recompute_run_id": run_id,
            "schema_version": "auris.label-fact-set/1",
            "trace_anchor": {
                "action_trace_id": ctx.trace_id,
                "root_trace_id": ctx.trace_id,
            },
        },
    )
    session.add(fact_set)
    session.flush()
    run = LabelRecomputeRun(
        recompute_run_id=run_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        status="requested",
        target_label_version_id=request.target_label_version_id,
        target_resource_version=target.resource_version,
        target_content_sha256=cast(str, target.content_sha256),
        mapping_bundle_id=mapping.mapping_bundle_id if mapping else None,
        mapping_bundle_sha256=mapping.canonical_manifest_sha256 if mapping else None,
        source_fact_set_id=request.source_fact_set_id,
        source_fact_namespace=request.source_fact_namespace,
        source_head_generation=request.source_head_generation,
        source_manifest_sha256=request.source_manifest_sha256,
        candidate_fact_set_id=fact_set_id,
        fact_namespace=request.fact_namespace,
        fact_as_of=request.fact_as_of,
        partition_scope=[partition.model_dump(mode="json") for partition in request.partitions],
        asset_scope=request.asset_scope,
        coverage_policy=request.coverage_policy,
        coverage_min=request.coverage_min,
        budget=request.budget,
        budget_units=request.budget_units,
        request_sha256=request_sha256,
        root_trace_id=ctx.trace_id,
        action_trace_id=ctx.trace_id,
        trace_id=ctx.trace_id,
        payload={
            "frozen_request": request_document,
            "source_fact_set_head_id": source_head.fact_set_head_id,
            "schema_version": "auris.label-recompute-run/1",
        },
    )
    session.add(run)
    session.flush()
    created_items: list[LabelRecomputeRunItem] = []
    for partition in request.partitions:
        item_id = _id(
            "lrri",
            {"partition_id": partition.partition_id, "recompute_run_id": run_id},
        )
        execution = _execution_run(
            ctx,
            recompute_run_id=run_id,
            item_id=item_id,
            partition_id=partition.partition_id,
            attempt_generation=1,
            request_sha256=request_sha256,
        )
        session.add(execution)
        session.flush()
        item = LabelRecomputeRunItem(
            recompute_run_item_id=item_id,
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            recompute_run_id=run_id,
            partition_id=partition.partition_id,
            status="queued",
            attempt_generation=1,
            execution_run_id=execution.run_id,
            row_count=0,
            lineage_manifest={},
            root_trace_id=ctx.trace_id,
            action_trace_id=ctx.trace_id,
            trace_id=ctx.trace_id,
            payload={"source_scope": partition.source_scope},
        )
        session.add(item)
        created_items.append(item)
    try:
        session.flush()
    except IntegrityError as error:
        raise ApiError(
            "LABEL_RECOMPUTE_CREATE_CONFLICT",
            "同一冻结请求已被并发创建",
            409,
            retryable=True,
        ) from error
    audit, outbox = _record_change(
        session,
        ctx,
        action="label_recompute_run.requested",
        object_type="label_recompute_run",
        object_id=run_id,
        before=None,
        after={
            "candidate_fact_set_id": fact_set_id,
            "fact_namespace": request.fact_namespace,
            "partition_count": len(created_items),
            "request_sha256": request_sha256,
            "resource_version": 1,
            "status": run.status,
            "target_label_version_id": run.target_label_version_id,
        },
    )
    response = _run_response(
        run,
        created_items,
        fact_set,
        ctx,
        audit_id=audit.audit_id,
        outbox_event_id=outbox.event_id,
    )
    return _save(
        session,
        ctx,
        operation=CREATE_OPERATION,
        body_hash=body_hash,
        status_code=201,
        response=response,
    )


def _execution_receipt(
    session: Session,
    ctx: RequestContext,
    item: LabelRecomputeRunItem,
    completion_receipt_id: str,
    expected_status: Literal["success", "failed"],
) -> tuple[RunRecord, RunCompletionReceipt]:
    execution = session.scalar(
        select(RunRecord).where(
            RunRecord.tenant_id == ctx.tenant_id,
            RunRecord.project_id == ctx.project_id,
            RunRecord.run_id == item.execution_run_id,
        )
    )
    receipt = session.scalar(
        select(RunCompletionReceipt).where(
            RunCompletionReceipt.tenant_id == ctx.tenant_id,
            RunCompletionReceipt.project_id == ctx.project_id,
            RunCompletionReceipt.run_id == item.execution_run_id,
            RunCompletionReceipt.completion_receipt_id == completion_receipt_id,
        )
    )
    if (
        execution is None
        or receipt is None
        or execution.run_type != "label_recompute_partition"
        or execution.status != expected_status
        or receipt.processing_state != "completed"
        or receipt.completion_status != expected_status
        or receipt.adapter != "dagster"
    ):
        raise ApiError(
            "LABEL_RECOMPUTE_EXECUTION_RECEIPT_INVALID",
            "分区完成不能证明已绑定执行运行的可信终态",
            409,
            details=[
                {
                    "execution_run_id": item.execution_run_id,
                    "expected_status": expected_status,
                }
            ],
        )
    anchor = execution.payload.get("label_recompute")
    expected_anchor = {
        "attempt_generation": item.attempt_generation,
        "recompute_run_id": item.recompute_run_id,
        "recompute_run_item_id": item.recompute_run_item_id,
    }
    if not isinstance(anchor, dict) or any(anchor.get(k) != v for k, v in expected_anchor.items()):
        raise ApiError(
            "LABEL_RECOMPUTE_EXECUTION_ANCHOR_DRIFT",
            "执行运行与重算分区的冻结锚点不一致",
            409,
        )
    return execution, receipt


def _candidate_lineage(
    session: Session,
    run: LabelRecomputeRun,
    execution: RunRecord,
    candidate: LabelRecomputeFactCandidate,
) -> tuple[LabelAggregate, list[LabelObservation], dict[str, Any]]:
    aggregate = session.scalar(
        select(LabelAggregate).where(
            LabelAggregate.tenant_id == run.tenant_id,
            LabelAggregate.project_id == run.project_id,
            LabelAggregate.aggregate_id == candidate.aggregate_id,
        )
    )
    if aggregate is None:
        raise ApiError("LABEL_RECOMPUTE_AGGREGATE_NOT_FOUND", "目标 Aggregate 不存在", 404)
    aggregation_run = session.scalar(
        select(LabelAggregationRun).where(
            LabelAggregationRun.tenant_id == run.tenant_id,
            LabelAggregationRun.project_id == run.project_id,
            LabelAggregationRun.aggregation_run_id == aggregate.aggregation_run_id,
        )
    )
    expected_binding = {
        "label_id": candidate.label_id,
        "label_version_id": run.target_label_version_id,
        "subject_key": candidate.subject_key,
        "subject_scope": candidate.subject_scope,
        "value_sha256": strict_canonical_sha256(candidate.value),
        "value_type": candidate.value_type,
    }
    actual_binding = {
        "label_id": aggregate.label_id,
        "label_version_id": aggregate.label_version_id,
        "subject_key": aggregate.subject_key,
        "subject_scope": aggregate.subject_scope,
        "value_sha256": strict_canonical_sha256(aggregate.value_json),
        "value_type": aggregate.value_type,
    }
    if (
        actual_binding != expected_binding
        or aggregation_run is None
        or aggregation_run.label_version_id != run.target_label_version_id
        or aggregate.trace_id != execution.trace_id
        or aggregation_run.trace_id != execution.trace_id
    ):
        raise ApiError(
            "LABEL_RECOMPUTE_AGGREGATE_LINEAGE_MISMATCH",
            "目标 Aggregate 未绑定当前执行或冻结目标标签版本",
            409,
            details=[{"actual": actual_binding, "expected": expected_binding}],
        )
    members = list(
        session.scalars(
            select(LabelAggregateMember).where(
                LabelAggregateMember.aggregate_id == aggregate.aggregate_id,
                LabelAggregateMember.included.is_(True),
            )
        )
    )
    member_ids = {member.observation_id for member in members}
    if member_ids != set(candidate.observation_ids):
        raise ApiError(
            "LABEL_RECOMPUTE_OBSERVATION_LINEAGE_INCOMPLETE",
            "提交的 Observation 集合必须与 Aggregate 的 included lineage 完全一致",
            409,
        )
    observations = list(
        session.scalars(
            select(LabelObservation).where(
                LabelObservation.tenant_id == run.tenant_id,
                LabelObservation.project_id == run.project_id,
                LabelObservation.observation_id.in_(candidate.observation_ids),
            )
        )
    )
    if len(observations) != len(candidate.observation_ids) or any(
        observation.label_version_id != run.target_label_version_id
        or observation.trace_id != execution.trace_id
        for observation in observations
    ):
        raise ApiError(
            "LABEL_RECOMPUTE_OBSERVATION_LINEAGE_MISMATCH",
            "Observation lineage 跨 scope、跨目标版本、缺行或不属于当前执行",
            409,
        )
    evidence_by_id = {
        observation.observation_id: observation.evidence_sha256 for observation in observations
    }
    if any(
        evidence_by_id.get(member.observation_id) != member.evidence_sha256 for member in members
    ):
        raise ApiError(
            "LABEL_RECOMPUTE_EVIDENCE_HASH_MISMATCH",
            "AggregateMember 与 Observation evidence SHA 不一致",
            409,
        )
    lineage = {
        "aggregate": {
            "aggregate_id": aggregate.aggregate_id,
            "deterministic_hash": aggregate.deterministic_hash,
        },
        "observations": [
            {
                "evidence_sha256": observation.evidence_sha256,
                "observation_id": observation.observation_id,
                "output_sha256": observation.output_sha256,
            }
            for observation in sorted(observations, key=lambda value: value.observation_id)
        ],
    }
    return aggregate, observations, lineage


def _validate_label_item(
    session: Session,
    run: LabelRecomputeRun,
    candidate: LabelRecomputeFactCandidate,
) -> None:
    item = session.scalar(
        select(LabelVersionItem).where(
            LabelVersionItem.tenant_id == run.tenant_id,
            LabelVersionItem.project_id == run.project_id,
            LabelVersionItem.label_version_id == run.target_label_version_id,
            LabelVersionItem.label_id == candidate.label_id,
        )
    )
    if item is None or item.value_type != candidate.value_type:
        raise ApiError(
            "LABEL_RECOMPUTE_LABEL_BINDING_INVALID",
            "候选事实未绑定目标版本中的同类型标签项",
            409,
        )


def _fact_documents(
    session: Session,
    run: LabelRecomputeRun,
    item: LabelRecomputeRunItem,
    execution: RunRecord,
    candidates: list[LabelRecomputeFactCandidate],
    recorded_at: datetime,
) -> list[tuple[LabelRecomputeFactCandidate, dict[str, Any], dict[str, Any]]]:
    documents: list[tuple[LabelRecomputeFactCandidate, dict[str, Any], dict[str, Any]]] = []
    seen_keys: set[str] = set()
    for candidate in candidates:
        _validate_label_item(session, run, candidate)
        _aggregate, _observations, lineage = _candidate_lineage(session, run, execution, candidate)
        logical_document = {
            "assertion_slot": candidate.assertion_slot,
            "event_or_segment_id": candidate.event_or_segment_id,
            "fact_namespace": run.fact_namespace,
            "label_id": candidate.label_id,
            "project_id": run.project_id,
            "schema_version": "auris.label-fact-logical-key/1",
            "subject_key": candidate.subject_key,
            "subject_scope": candidate.subject_scope,
            "tenant_id": run.tenant_id,
        }
        logical_sha = strict_canonical_sha256(logical_document)
        if logical_sha in seen_keys:
            raise ApiError(
                "LABEL_RECOMPUTE_DUPLICATE_LOGICAL_FACT",
                "同一分区不能生成重复 logical fact key",
                409,
            )
        seen_keys.add(logical_sha)
        document = {
            "action_trace_id": item.action_trace_id,
            "assertion_slot": candidate.assertion_slot,
            "authority": "recomputed",
            "event_or_segment_id": candidate.event_or_segment_id,
            "fact_namespace": run.fact_namespace,
            "fact_set_id": run.candidate_fact_set_id,
            "label_id": candidate.label_id,
            "label_version_id": run.target_label_version_id,
            "lineage": lineage,
            "logical_key_sha256": logical_sha,
            "occurred_at": _iso(candidate.occurred_at),
            "occurred_at_origin": "source",
            "partition_id": item.partition_id,
            "project_id": run.project_id,
            "recompute_run_item_id": item.recompute_run_item_id,
            "recorded_at": _iso(recorded_at),
            "revision": 1,
            "root_trace_id": run.root_trace_id,
            "schema_version": "auris.label-fact-revision/1",
            "source_kind": "recompute-run-item",
            "subject_key": candidate.subject_key,
            "subject_scope": candidate.subject_scope,
            "tenant_id": run.tenant_id,
            "value": candidate.value,
            "value_type": candidate.value_type,
        }
        documents.append((candidate, logical_document, document))
    return documents


def _materialize_facts(
    session: Session,
    ctx: RequestContext,
    run: LabelRecomputeRun,
    item: LabelRecomputeRunItem,
    execution: RunRecord,
    candidates: list[LabelRecomputeFactCandidate],
) -> list[LabelFact]:
    recorded_at = _now()
    documents = _fact_documents(session, run, item, execution, candidates, recorded_at)
    facts: list[LabelFact] = []
    for candidate, logical_document, document in documents:
        logical_sha = strict_canonical_sha256(logical_document)
        content_sha = strict_canonical_sha256(document)
        fact_id = _id(
            "lf",
            {
                "content_sha256": content_sha,
                "logical_key_sha256": logical_sha,
                "recompute_run_item_id": item.recompute_run_item_id,
            },
        )
        fact = LabelFact(
            fact_id=fact_id,
            tenant_id=run.tenant_id,
            project_id=run.project_id,
            aggregate_id=None,
            supersedes_fact_id=None,
            fact_namespace=run.fact_namespace,
            logical_key_sha=logical_sha,
            revision=1,
            event_or_segment_id=candidate.event_or_segment_id,
            assertion_slot=candidate.assertion_slot,
            occurred_at=candidate.occurred_at,
            recorded_at=recorded_at,
            occurred_at_origin="source",
            source_kind="recompute-run-item",
            human_review_decision_id=None,
            recompute_run_item_id=item.recompute_run_item_id,
            fact_set_id=run.candidate_fact_set_id,
            content_sha256=content_sha,
            root_trace_id=run.root_trace_id,
            action_trace_id=ctx.trace_id,
            label_version_id=run.target_label_version_id,
            subject_scope=candidate.subject_scope,
            subject_key=candidate.subject_key,
            label_id=candidate.label_id,
            value_type=candidate.value_type,
            value_json=candidate.value,
            authority="recomputed",
            status="recorded",
            active_slot=None,
            review_decision_id=None,
            trace_id=execution.trace_id,
            payload={
                "lineage": document["lineage"],
                "logical_key_document": logical_document,
                "partition_id": item.partition_id,
                "schema_version": "auris.label-fact-revision/1",
            },
        )
        session.add(fact)
        facts.append(fact)
    try:
        session.flush()
    except IntegrityError as error:
        raise ApiError(
            "LABEL_RECOMPUTE_FACT_CONFLICT",
            "候选事实与已有 logical key、来源或 scope 冲突",
            409,
        ) from error
    return facts


def _item_manifest_from_facts(
    item: LabelRecomputeRunItem,
    facts: list[LabelFact],
) -> dict[str, Any]:
    lineage = [
        {
            "content_sha256": fact.content_sha256,
            "fact_id": fact.fact_id,
            "lineage": fact.payload.get("lineage"),
            "logical_key_sha256": fact.logical_key_sha,
        }
        for fact in sorted(facts, key=lambda value: value.fact_id)
    ]
    source_sha = strict_canonical_sha256(
        [{"fact_id": value["fact_id"], "lineage": value["lineage"]} for value in lineage]
    )
    result_sha = strict_canonical_sha256(
        [
            {
                "content_sha256": value["content_sha256"],
                "fact_id": value["fact_id"],
                "logical_key_sha256": value["logical_key_sha256"],
            }
            for value in lineage
        ]
    )
    manifest = {
        "partition_id": item.partition_id,
        "result_manifest_sha256": result_sha,
        "row_count": len(facts),
        "schema_version": "auris.label-recompute-partition-manifest/1",
        "source_manifest_sha256": source_sha,
        "status": "succeeded",
    }
    return {
        **manifest,
        "content_sha256": strict_canonical_sha256(manifest),
        "lineage": lineage,
    }


def _facts_for_item(
    session: Session,
    run: LabelRecomputeRun,
    item: LabelRecomputeRunItem,
) -> list[LabelFact]:
    return list(
        session.scalars(
            select(LabelFact)
            .where(
                LabelFact.tenant_id == run.tenant_id,
                LabelFact.project_id == run.project_id,
                LabelFact.fact_set_id == run.candidate_fact_set_id,
                LabelFact.fact_namespace == run.fact_namespace,
                LabelFact.recompute_run_item_id == item.recompute_run_item_id,
                LabelFact.source_kind == "recompute-run-item",
            )
            .order_by(LabelFact.fact_id)
        )
    )


def _assert_item_matches_facts(
    session: Session,
    run: LabelRecomputeRun,
    item: LabelRecomputeRunItem,
) -> dict[str, Any]:
    facts = _facts_for_item(session, run, item)
    actual = _item_manifest_from_facts(item, facts)
    stored = {
        "content_sha256": item.content_sha256,
        "result_manifest_sha256": item.result_manifest_sha256,
        "row_count": item.row_count,
        "source_manifest_sha256": item.source_manifest_sha256,
    }
    expected = {key: actual[key] for key in stored}
    if stored != expected:
        raise ApiError(
            "LABEL_RECOMPUTE_ITEM_MANIFEST_DRIFT",
            "分区 manifest 与实际 append-only LabelFact 行不一致",
            409,
            details=[{"actual": expected, "stored": stored}],
        )
    return actual


def _complete_fact_set(
    session: Session,
    run: LabelRecomputeRun,
    items: list[LabelRecomputeRunItem],
    fact_set: LabelFactSet,
) -> None:
    manifests = [_assert_item_matches_facts(session, run, item) for item in items]
    total_rows = sum(cast(int, manifest["row_count"]) for manifest in manifests)
    if total_rows > run.budget_units:
        run.status = "blocked"
        raise ApiError(
            "LABEL_RECOMPUTE_BUDGET_EXCEEDED",
            "实际候选事实行数超过冻结预算",
            409,
            details=[{"actual_units": total_rows, "budget_units": run.budget_units}],
        )
    partition_manifest = {
        "partitions": [
            {
                key: manifest[key]
                for key in (
                    "content_sha256",
                    "partition_id",
                    "result_manifest_sha256",
                    "row_count",
                    "source_manifest_sha256",
                    "status",
                )
            }
            for manifest in manifests
        ],
        "schema_version": "auris.label-recompute-partitions/1",
    }
    partition_sha = strict_canonical_sha256(partition_manifest)
    source_sha = strict_canonical_sha256(
        {
            "item_source_manifests": [manifest["source_manifest_sha256"] for manifest in manifests],
            "source_fact_set_manifest_sha256": run.source_manifest_sha256,
        }
    )
    result_sha = strict_canonical_sha256(
        [manifest["result_manifest_sha256"] for manifest in manifests]
    )
    target_anchor = {
        "artifact_status": fact_set.payload["frozen_manifest"]["target_label_version"][
            "artifact_status"
        ],
        "content_sha256": run.target_content_sha256,
        "label_version_id": run.target_label_version_id,
        "resource_version": run.target_resource_version,
    }
    frozen_manifest = {
        "fact_as_of": _iso(run.fact_as_of),
        "fact_namespace": run.fact_namespace,
        "partition_manifest": partition_manifest,
        "partition_manifest_sha256": partition_sha,
        "project_id": run.project_id,
        "result_manifest_sha256": result_sha,
        "row_count": total_rows,
        "schema_version": "auris.label-fact-set-manifest/1",
        "source_manifest_sha256": source_sha,
        "target_label_version": target_anchor,
        "tenant_id": run.tenant_id,
    }
    fact_set.partition_manifest = partition_manifest
    fact_set.partition_manifest_sha256 = partition_sha
    fact_set.source_manifest_sha256 = source_sha
    fact_set.result_manifest_sha256 = result_sha
    fact_set.row_count = total_rows
    fact_set.manifest_sha256 = strict_canonical_sha256(frozen_manifest)
    fact_set.action_trace_id = run.action_trace_id
    fact_set.payload = {
        **fact_set.payload,
        "frozen_manifest": frozen_manifest,
        "materialization_status": "complete",
        "source_anchor": {
            "fact_set_id": run.source_fact_set_id,
            "generation": run.source_head_generation,
            "manifest_sha256": run.source_manifest_sha256,
        },
    }
    run.status = "candidate-complete"


def complete_label_recompute_run_item(
    session: Session,
    ctx: RequestContext,
    run_id: str,
    item_id: str,
    request: LabelRecomputeRunItemCompletionRequest,
) -> dict[str, Any]:
    require_any_role(ctx, _WORKER_ROLES, action="label-recompute-run-items.complete")
    body_hash = _request_hash(
        ctx,
        operation=COMPLETE_OPERATION,
        resource_id=item_id,
        body=request.model_dump(mode="json"),
    )
    operation = f"{COMPLETE_OPERATION}:{item_id}:{request.attempt_generation}"
    replay = _replay(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    run = _load_run(session, ctx, run_id, for_update=True)
    item = _load_item(session, ctx, run_id, item_id, for_update=True)
    if item.attempt_generation != request.attempt_generation or item.status not in {
        "queued",
        "running",
    }:
        raise ApiError(
            "LABEL_RECOMPUTE_ITEM_ATTEMPT_CONFLICT",
            "分区 attempt generation 或状态已变化",
            409,
            details=[
                {
                    "actual_attempt_generation": item.attempt_generation,
                    "status": item.status,
                }
            ],
        )
    execution, _receipt = _execution_receipt(
        session,
        ctx,
        item,
        request.completion_receipt_id,
        request.status,
    )
    before = {
        "attempt_generation": item.attempt_generation,
        "status": item.status,
    }
    fact_set = session.scalar(
        select(LabelFactSet)
        .where(
            LabelFactSet.tenant_id == ctx.tenant_id,
            LabelFactSet.project_id == ctx.project_id,
            LabelFactSet.fact_set_id == run.candidate_fact_set_id,
        )
        .with_for_update()
    )
    if fact_set is None:
        raise ApiError("LABEL_RECOMPUTE_CANDIDATE_FACT_SET_MISSING", "候选 FactSet 不存在", 409)
    if request.status == "failed":
        item.status = "failed"
        item.completion_receipt_id = request.completion_receipt_id
        item.payload = {
            **item.payload,
            "error_code": request.error_code,
            "retryable": request.retryable,
        }
        session.flush()
        all_items = _items(session, run, for_update=True)
        succeeded = sum(value.status == "succeeded" for value in all_items)
        run.status = "partial-failed" if succeeded else "failed"
    else:
        facts = _materialize_facts(session, ctx, run, item, execution, request.facts)
        manifest = _item_manifest_from_facts(item, facts)
        item.status = "succeeded"
        item.completion_receipt_id = request.completion_receipt_id
        item.source_manifest_sha256 = cast(str, manifest["source_manifest_sha256"])
        item.result_manifest_sha256 = cast(str, manifest["result_manifest_sha256"])
        item.content_sha256 = cast(str, manifest["content_sha256"])
        item.row_count = cast(int, manifest["row_count"])
        item.lineage_manifest = {"facts": manifest["lineage"]}
        item.action_trace_id = ctx.trace_id
        session.flush()
        all_items = _items(session, run, for_update=True)
        if all(value.status == "succeeded" for value in all_items):
            _complete_fact_set(session, run, all_items, fact_set)
        elif any(value.status == "failed" for value in all_items):
            run.status = "partial-failed"
        else:
            run.status = "running"
    session.flush()
    audit, outbox = _record_change(
        session,
        ctx,
        action=(
            "label_recompute_run_item.succeeded"
            if item.status == "succeeded"
            else "label_recompute_run_item.failed"
        ),
        object_type="label_recompute_run_item",
        object_id=item.recompute_run_item_id,
        before=before,
        after={
            "attempt_generation": item.attempt_generation,
            "candidate_fact_set_id": run.candidate_fact_set_id,
            "content_sha256": item.content_sha256,
            "partition_id": item.partition_id,
            "recompute_run_id": run.recompute_run_id,
            "resource_version": item.attempt_generation,
            "result_manifest_sha256": item.result_manifest_sha256,
            "row_count": item.row_count,
            "run_status": run.status,
            "source_manifest_sha256": item.source_manifest_sha256,
            "status": item.status,
        },
    )
    response = _item_response(
        run,
        item,
        fact_set,
        ctx,
        audit_id=audit.audit_id,
        outbox_event_id=outbox.event_id,
    )
    return _save(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=200,
        response=response,
    )


def retry_label_recompute_run_item(
    session: Session,
    ctx: RequestContext,
    run_id: str,
    item_id: str,
    request: LabelRecomputeRunItemRetryRequest,
) -> dict[str, Any]:
    require_any_role(ctx, _WRITER_ROLES, action="label-recompute-run-items.retry")
    operation = f"{RETRY_OPERATION}:{item_id}"
    body_hash = _request_hash(
        ctx,
        operation=operation,
        resource_id=item_id,
        body=request.model_dump(mode="json"),
    )
    replay = _replay(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    run = _load_run(session, ctx, run_id, for_update=True)
    item = _load_item(session, ctx, run_id, item_id, for_update=True)
    if (
        item.status != "failed"
        or item.attempt_generation != request.expected_attempt_generation
        or item.payload.get("retryable") is not True
    ):
        raise ApiError(
            "LABEL_RECOMPUTE_RETRY_CONFLICT",
            "仅可重试被执行回执标记为 retryable 的当前失败 attempt",
            409,
        )
    before = {"attempt_generation": item.attempt_generation, "status": item.status}
    next_attempt = item.attempt_generation + 1
    execution = _execution_run(
        ctx,
        recompute_run_id=run.recompute_run_id,
        item_id=item.recompute_run_item_id,
        partition_id=item.partition_id,
        attempt_generation=next_attempt,
        request_sha256=run.request_sha256,
    )
    session.add(execution)
    session.flush()
    item.status = "queued"
    item.attempt_generation = next_attempt
    item.execution_run_id = execution.run_id
    item.completion_receipt_id = None
    item.source_manifest_sha256 = None
    item.result_manifest_sha256 = None
    item.content_sha256 = None
    item.row_count = 0
    item.lineage_manifest = {}
    item.action_trace_id = ctx.trace_id
    item.payload = {"source_scope": item.payload.get("source_scope")}
    run.status = "running"
    session.flush()
    fact_set = session.scalar(
        select(LabelFactSet).where(
            LabelFactSet.tenant_id == ctx.tenant_id,
            LabelFactSet.project_id == ctx.project_id,
            LabelFactSet.fact_set_id == run.candidate_fact_set_id,
        )
    )
    assert fact_set is not None
    audit, outbox = _record_change(
        session,
        ctx,
        action="label_recompute_run_item.retried",
        object_type="label_recompute_run_item",
        object_id=item.recompute_run_item_id,
        before=before,
        after={
            "attempt_generation": next_attempt,
            "partition_id": item.partition_id,
            "recompute_run_id": run.recompute_run_id,
            "resource_version": next_attempt,
            "status": item.status,
        },
    )
    response = _item_response(
        run,
        item,
        fact_set,
        ctx,
        audit_id=audit.audit_id,
        outbox_event_id=outbox.event_id,
    )
    return _save(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=200,
        response=response,
    )


def assert_recompute_fact_set_materialized(
    session: Session,
    ctx: RequestContext,
    fact_set: LabelFactSet,
) -> None:
    """Re-query append-only rows and item receipts before validate/promote."""

    run_id = fact_set.payload.get("recompute_run_id")
    if not isinstance(run_id, str):
        return
    run = _load_run(session, ctx, run_id, for_update=True)
    if run.candidate_fact_set_id != fact_set.fact_set_id or run.status != "candidate-complete":
        raise ApiError(
            "LABEL_RECOMPUTE_FACT_SET_INCOMPLETE",
            "重算候选 FactSet 尚未完成全部分区",
            409,
        )
    items = _items(session, run, for_update=True)
    expected_partition_ids = {
        str(partition.get("partition_id")) for partition in run.partition_scope
    }
    if (
        not items
        or {item.partition_id for item in items} != expected_partition_ids
        or any(item.status != "succeeded" for item in items)
    ):
        raise ApiError(
            "LABEL_RECOMPUTE_PARTITION_COVERAGE_INCOMPLETE",
            "重算分区缺失、额外或未成功，禁止验证/晋级",
            409,
        )
    manifests = [_assert_item_matches_facts(session, run, item) for item in items]
    scoped_facts = int(
        session.scalar(
            select(func.count())
            .select_from(LabelFact)
            .where(
                LabelFact.tenant_id == run.tenant_id,
                LabelFact.project_id == run.project_id,
                LabelFact.fact_set_id == fact_set.fact_set_id,
                LabelFact.fact_namespace == run.fact_namespace,
            )
        )
        or 0
    )
    item_facts = sum(cast(int, manifest["row_count"]) for manifest in manifests)
    if scoped_facts != item_facts or fact_set.row_count != item_facts:
        raise ApiError(
            "LABEL_RECOMPUTE_FACT_SET_ROW_COVERAGE_MISMATCH",
            "FactSet 存在缺行、额外行或 row_count 漂移",
            409,
            details=[
                {
                    "actual_fact_rows": scoped_facts,
                    "fact_set_row_count": fact_set.row_count,
                    "item_fact_rows": item_facts,
                }
            ],
        )
    # Rebuild the global manifest in-memory and compare every persisted anchor.
    probe = LabelFactSet(
        fact_set_id=fact_set.fact_set_id,
        tenant_id=fact_set.tenant_id,
        project_id=fact_set.project_id,
        fact_namespace=fact_set.fact_namespace,
        target_label_version_id=fact_set.target_label_version_id,
        status=fact_set.status,
        fact_as_of=fact_set.fact_as_of,
        partition_manifest=fact_set.partition_manifest,
        partition_manifest_sha256=fact_set.partition_manifest_sha256,
        source_manifest_sha256=fact_set.source_manifest_sha256,
        result_manifest_sha256=fact_set.result_manifest_sha256,
        row_count=fact_set.row_count,
        manifest_sha256=fact_set.manifest_sha256,
        root_trace_id=fact_set.root_trace_id,
        action_trace_id=fact_set.action_trace_id,
        trace_id=fact_set.trace_id,
        payload=dict(fact_set.payload),
    )
    _complete_fact_set(session, run, items, probe)
    actual = {
        "manifest_sha256": fact_set.manifest_sha256,
        "partition_manifest": fact_set.partition_manifest,
        "partition_manifest_sha256": fact_set.partition_manifest_sha256,
        "result_manifest_sha256": fact_set.result_manifest_sha256,
        "source_manifest_sha256": fact_set.source_manifest_sha256,
    }
    expected = {key: getattr(probe, key) for key in actual}
    if actual != expected:
        raise ApiError(
            "LABEL_RECOMPUTE_FACT_SET_MANIFEST_DRIFT",
            "FactSet 全局 manifest 与实际分区/事实不一致",
            409,
            details=[{"actual": actual, "expected": expected}],
        )
