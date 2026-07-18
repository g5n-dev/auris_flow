from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.domain.label_mapping import sha256_document
from app.models import (
    HumanReviewDecision,
    HumanReviewTask,
    JsonResource,
    LabelAggregate,
    LabelMappingBundle,
    LabelMappingBundlePath,
    LabelVersion,
    LabelVersionItem,
    ListeningAnnotation,
    ReleaseBundleHead,
)
from app.schemas.manual_label_drafts import (
    ManualLabelDraftCreateRequest,
    ManualLabelDraftRebaseRequest,
    ManualLabelDraftSubmitRequest,
)
from app.services.audit_service import record_audit
from app.services.label_closed_loop_service import _create_label_fact
from app.services.outbox_service import enqueue_event
from app.services.resource_service import upsert_resource


def _iso(value: datetime) -> str:
    normalized = value
    if normalized.tzinfo is None or normalized.utcoffset() is None:
        normalized = normalized.replace(tzinfo=UTC)
    return normalized.astimezone(UTC).isoformat().replace("+00:00", "Z")


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
            "MANUAL_LABEL_VALUE_TYPE_INVALID",
            "人工标签值不符合冻结标签项的 value_type",
            422,
            details=[{"value_type": kind}],
        )


def _production_head(
    session: Session,
    ctx: RequestContext,
    *,
    expected_generation: int,
) -> ReleaseBundleHead:
    head = session.scalar(
        select(ReleaseBundleHead)
        .where(
            ReleaseBundleHead.tenant_id == ctx.tenant_id,
            ReleaseBundleHead.project_id == ctx.project_id,
            ReleaseBundleHead.environment == "production",
        )
        .with_for_update()
    )
    if head is None:
        raise ApiError(
            "MANUAL_LABEL_RELEASE_HEAD_NOT_FOUND",
            "生产环境尚未形成可冻结的 ReleaseBundleHead",
            409,
        )
    if head.generation != expected_generation:
        raise ApiError(
            "MANUAL_LABEL_RELEASE_HEAD_CONFLICT",
            "生产 ReleaseBundleHead generation 已变化",
            409,
            details=[
                {
                    "actual_generation": head.generation,
                    "expected_generation": expected_generation,
                }
            ],
        )
    return head


def _label_item(
    session: Session,
    ctx: RequestContext,
    *,
    label_version_id: str,
    label_id: str,
) -> tuple[LabelVersion, LabelVersionItem]:
    version = session.scalar(
        select(LabelVersion).where(
            LabelVersion.tenant_id == ctx.tenant_id,
            LabelVersion.project_id == ctx.project_id,
            LabelVersion.label_version_id == label_version_id,
        )
    )
    item = session.scalar(
        select(LabelVersionItem).where(
            LabelVersionItem.tenant_id == ctx.tenant_id,
            LabelVersionItem.project_id == ctx.project_id,
            LabelVersionItem.label_version_id == label_version_id,
            LabelVersionItem.label_id == label_id,
        )
    )
    if version is None or item is None:
        raise ApiError(
            "MANUAL_LABEL_BINDING_NOT_FOUND",
            "人工 draft 引用的标签版本或标签项不存在",
            404,
        )
    if item.status != "active":
        raise ApiError(
            "MANUAL_LABEL_ITEM_NOT_ACTIVE",
            "人工 draft 只能绑定版本内 active 标签项",
            409,
            details=[{"status": item.status}],
        )
    return version, item


def _draft_summary(payload: dict[str, Any]) -> dict[str, Any]:
    draft = payload["draft_document"]
    return {
        "annotation_id": payload["annotation_id"],
        "audio_session_id": payload["audio_session_id"],
        "draft_sha256": payload["draft_sha256"],
        "event_or_segment_id": draft["event_or_segment_id"],
        "evidence_sha256": draft["evidence_ref"]["sha256"],
        "label_id": draft["label_id"],
        "label_version_id": draft["label_version_id"],
        "occurred_at": draft["occurred_at"],
        "release_head_generation": payload["release_head_generation"],
        "status": payload["status"],
    }


def _persist_new_draft(
    session: Session,
    ctx: RequestContext,
    *,
    audio_session_id: str,
    request: ManualLabelDraftCreateRequest,
    head: ReleaseBundleHead,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    existing = session.get(ListeningAnnotation, request.annotation_id)
    existing_resource = session.scalar(
        select(JsonResource).where(
            JsonResource.tenant_id == ctx.tenant_id,
            JsonResource.project_id == ctx.project_id,
            JsonResource.collection == "listening_annotations",
            JsonResource.resource_key == request.annotation_id,
        )
    )
    if existing is not None or existing_resource is not None:
        raise ApiError(
            "MANUAL_LABEL_DRAFT_ALREADY_EXISTS",
            "annotation_id 已存在；人工 draft 不允许覆盖",
            409,
        )
    draft_document = {
        **request.model_dump(mode="json"),
        "annotation_kind": "label-fact-draft",
        "audio_session_id": audio_session_id,
        "schema_version": "auris.manual-label-draft/1",
    }
    draft_sha256 = sha256_document(draft_document)
    payload = {
        "annotation_id": request.annotation_id,
        "annotation_kind": "label-fact-draft",
        "audio_session_id": audio_session_id,
        "draft_document": draft_document,
        "draft_sha256": draft_sha256,
        "release_bundle_sha256": head.active_bundle_sha256,
        "release_head_generation": head.generation,
        "release_head_id": head.release_head_id,
        "root_trace_id": ctx.trace_id,
        "status": "draft",
        "trace_id": ctx.trace_id,
        **({"rebase_provenance": provenance} if provenance else {}),
    }
    projection = ListeningAnnotation(
        annotation_id=request.annotation_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        audio_session_id=audio_session_id,
        status="draft",
        trace_id=ctx.trace_id,
        payload=payload,
    )
    session.add(projection)
    upsert_resource(
        session,
        ctx,
        "listening_annotations",
        request.annotation_id,
        payload,
        status="draft",
        trace_id=ctx.trace_id,
    )
    summary = _draft_summary(payload)
    audit = record_audit(
        session,
        ctx,
        action="manual_label_draft.created",
        object_type="listening_annotation",
        object_id=request.annotation_id,
        after=summary,
    )
    outbox = enqueue_event(
        session,
        ctx,
        event_type="manual_label_draft.created",
        aggregate_type="listening_annotation",
        aggregate_id=request.annotation_id,
        payload=summary,
    )
    session.flush()
    return {
        **summary,
        "audit_id": audit.audit_id,
        "outbox_event_id": outbox.event_id,
        "trace_id": ctx.trace_id,
    }


def create_manual_label_draft(
    session: Session,
    ctx: RequestContext,
    *,
    audio_session_id: str,
    request: ManualLabelDraftCreateRequest,
) -> dict[str, Any]:
    head = _production_head(
        session,
        ctx,
        expected_generation=request.expected_release_head_generation,
    )
    if head.label_version_id != request.label_version_id:
        raise ApiError(
            "STALE_LABEL_VERSION",
            "人工 draft 必须冻结当前生产 Head 的标签版本",
            409,
            details=[
                {
                    "current_label_version_id": head.label_version_id,
                    "draft_label_version_id": request.label_version_id,
                    "rebase_required": True,
                }
            ],
        )
    _version, item = _label_item(
        session,
        ctx,
        label_version_id=request.label_version_id,
        label_id=request.label_id,
    )
    if item.value_type != request.value_type:
        raise ApiError(
            "MANUAL_LABEL_VALUE_TYPE_MISMATCH",
            "人工 draft value_type 与冻结标签项不一致",
            409,
        )
    _validate_label_value(item.value_type, request.value)
    return _persist_new_draft(
        session,
        ctx,
        audio_session_id=audio_session_id,
        request=request,
        head=head,
    )


def _scoped_draft(
    session: Session,
    ctx: RequestContext,
    *,
    audio_session_id: str,
    annotation_id: str,
    for_update: bool,
) -> ListeningAnnotation:
    statement = select(ListeningAnnotation).where(
        ListeningAnnotation.tenant_id == ctx.tenant_id,
        ListeningAnnotation.project_id == ctx.project_id,
        ListeningAnnotation.audio_session_id == audio_session_id,
        ListeningAnnotation.annotation_id == annotation_id,
    )
    if for_update:
        statement = statement.with_for_update()
    draft = session.scalar(statement)
    if draft is None or draft.payload.get("annotation_kind") != "label-fact-draft":
        raise ApiError("MANUAL_LABEL_DRAFT_NOT_FOUND", "人工标签 draft 不存在", 404)
    return draft


def submit_manual_label_draft(
    session: Session,
    ctx: RequestContext,
    *,
    audio_session_id: str,
    annotation_id: str,
    request: ManualLabelDraftSubmitRequest,
) -> dict[str, Any]:
    draft = _scoped_draft(
        session,
        ctx,
        audio_session_id=audio_session_id,
        annotation_id=annotation_id,
        for_update=True,
    )
    if draft.status != "draft":
        raise ApiError(
            "MANUAL_LABEL_DRAFT_ALREADY_SUBMITTED",
            "人工标签 draft 已提交，不能重复制造权威事实",
            409,
            details=[{"status": draft.status}],
        )
    if draft.payload.get("draft_sha256") != request.expected_draft_sha256:
        raise ApiError(
            "MANUAL_LABEL_DRAFT_CONTENT_CONFLICT",
            "人工标签 draft 内容哈希已变化",
            409,
        )
    document = draft.payload.get("draft_document")
    if not isinstance(document, dict):
        raise ApiError("MANUAL_LABEL_DRAFT_DRIFT", "人工标签 draft 文档缺失", 409)
    head = _production_head(
        session,
        ctx,
        expected_generation=request.expected_release_head_generation,
    )
    if head.label_version_id != document.get("label_version_id"):
        raise ApiError(
            "STALE_LABEL_VERSION",
            "draft 标签版本已不属于当前生产 Head；必须显式 rebase",
            409,
            details=[
                {
                    "current_label_version_id": head.label_version_id,
                    "draft_label_version_id": document.get("label_version_id"),
                    "rebase_required": True,
                }
            ],
        )
    _version, item = _label_item(
        session,
        ctx,
        label_version_id=str(document["label_version_id"]),
        label_id=str(document["label_id"]),
    )
    if item.value_type != document.get("value_type"):
        raise ApiError("MANUAL_LABEL_DRAFT_DRIFT", "draft value_type 已漂移", 409)
    _validate_label_value(item.value_type, document.get("value"))

    id_digest = sha256_document(
        {
            "annotation_id": annotation_id,
            "draft_sha256": request.expected_draft_sha256,
            "project_id": ctx.project_id,
            "tenant_id": ctx.tenant_id,
        }
    )[:24]
    aggregate_id = f"lagg_manual_{id_digest}"
    review_task_id = f"hrt_manual_{id_digest}"
    decision_id = f"hrd_manual_{id_digest}"
    for model, identifier in (
        (LabelAggregate, aggregate_id),
        (HumanReviewTask, review_task_id),
        (HumanReviewDecision, decision_id),
    ):
        if session.get(model, identifier) is not None:
            raise ApiError(
                "MANUAL_LABEL_SUBMISSION_DRIFT",
                "人工标签提交的确定性权威对象已存在但 draft 仍为未提交",
                409,
            )

    root_trace_id = str(draft.payload.get("root_trace_id") or draft.trace_id or ctx.trace_id)
    aggregate = LabelAggregate(
        aggregate_id=aggregate_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        aggregation_run_id=f"manual-label:{annotation_id}",
        label_version_id=str(document["label_version_id"]),
        policy_version_id="manual-label-draft/1",
        calibration_version_ids=[],
        subject_scope=str(document["subject_scope"]),
        subject_key=str(document["subject_key"]),
        label_id=str(document["label_id"]),
        value_type=str(document["value_type"]),
        value_json=document.get("value"),
        score=1.0,
        margin=1.0,
        risk_level=item.risk_level,
        decision="require_review",
        status="accepted",
        reason_codes=["manual-label-submission"],
        explanation={
            "annotation_id": annotation_id,
            "assertion_slot": document["assertion_slot"],
            "event_or_segment_id": document["event_or_segment_id"],
            "evidence_ref": document["evidence_ref"],
            "occurred_at": document["occurred_at"],
        },
        bucket_sha256=sha256_document(
            [document["subject_scope"], document["subject_key"], document["label_id"]]
        ),
        deterministic_hash=sha256_document(document),
        review_task_id=review_task_id,
        trace_id=root_trace_id,
    )
    session.add(aggregate)
    session.add(
        HumanReviewTask(
            review_task_id=review_task_id,
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            status="success",
            trace_id=root_trace_id,
            payload={
                "annotation_id": annotation_id,
                "source": "manual-label-draft",
            },
        )
    )
    session.flush()
    decision_payload = {
        "affected_objects": [{"type": "label_aggregate", "id": aggregate_id}],
        "after_json": {
            "targets": {
                f"label_aggregates:{aggregate_id}": {
                    "aggregate_id": aggregate_id,
                    "label_id": aggregate.label_id,
                    "label_version_id": aggregate.label_version_id,
                    "value": aggregate.value_json,
                    "value_type": aggregate.value_type,
                }
            }
        },
        "annotation_id": annotation_id,
        "decided_at": _iso(datetime.now(UTC)),
        "decided_by": ctx.user_id,
        "decision": "accepted",
        "decision_id": decision_id,
        "review_task_id": review_task_id,
        "source": "manual-label-draft",
    }
    session.add(
        HumanReviewDecision(
            decision_id=decision_id,
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            review_task_id=review_task_id,
            terminal_review_task_id=review_task_id,
            status="success",
            trace_id=root_trace_id,
            payload=decision_payload,
        )
    )
    session.flush()
    fact = _create_label_fact(
        session,
        ctx,
        aggregate,
        authority="human-confirmed",
        review_decision_id=decision_id,
        value=aggregate.value_json,
    )
    session.flush()
    submission = {
        "action_trace_id": ctx.trace_id,
        "decision_id": decision_id,
        "fact_id": fact.fact_id,
        "recorded_at": _iso(fact.recorded_at or datetime.now(UTC)),
        "root_trace_id": root_trace_id,
    }
    draft.status = "submitted"
    draft.trace_id = root_trace_id
    draft.payload = {
        **draft.payload,
        "status": "submitted",
        "submission": submission,
    }
    upsert_resource(
        session,
        ctx,
        "listening_annotations",
        annotation_id,
        dict(draft.payload),
        status="submitted",
        trace_id=root_trace_id,
    )
    summary = {
        **_draft_summary(draft.payload),
        **submission,
    }
    audit = record_audit(
        session,
        ctx,
        action="manual_label_draft.submitted",
        object_type="listening_annotation",
        object_id=annotation_id,
        after=summary,
        trace_id=root_trace_id,
    )
    outbox = enqueue_event(
        session,
        ctx,
        event_type="manual_label_draft.submitted",
        aggregate_type="listening_annotation",
        aggregate_id=annotation_id,
        payload=summary,
    )
    session.flush()
    return {
        **summary,
        "audit_id": audit.audit_id,
        "outbox_event_id": outbox.event_id,
        "trace_id": root_trace_id,
    }


def _rebase_preview(
    session: Session,
    ctx: RequestContext,
    *,
    draft: ListeningAnnotation,
    request: ManualLabelDraftRebaseRequest,
    head: ReleaseBundleHead,
) -> tuple[dict[str, Any], str | None]:
    document = draft.payload.get("draft_document")
    if not isinstance(document, dict):
        raise ApiError("MANUAL_LABEL_DRAFT_DRIFT", "人工标签 draft 文档缺失", 409)
    source_version_id = str(document["label_version_id"])
    if source_version_id == head.label_version_id:
        raise ApiError(
            "MANUAL_LABEL_REBASE_NOT_REQUIRED",
            "draft 已绑定当前生产标签版本，无需 rebase",
            409,
        )
    bundle = session.scalar(
        select(LabelMappingBundle).where(
            LabelMappingBundle.tenant_id == ctx.tenant_id,
            LabelMappingBundle.project_id == ctx.project_id,
            LabelMappingBundle.mapping_bundle_id == request.mapping_bundle_id,
        )
    )
    if (
        bundle is None
        or bundle.status != "published"
        or source_version_id not in bundle.source_label_version_ids
        or bundle.target_label_version_id != head.label_version_id
    ):
        raise ApiError(
            "MANUAL_LABEL_REBASE_BUNDLE_MISMATCH",
            "已发布 Mapping Bundle 不能解释 draft 到当前 Head 的变更",
            409,
        )
    paths = list(
        session.scalars(
            select(LabelMappingBundlePath).where(
                LabelMappingBundlePath.tenant_id == ctx.tenant_id,
                LabelMappingBundlePath.project_id == ctx.project_id,
                LabelMappingBundlePath.mapping_bundle_id == bundle.mapping_bundle_id,
                LabelMappingBundlePath.source_label_version_id == source_version_id,
                LabelMappingBundlePath.source_label_id == document["label_id"],
            )
        )
    )
    path_targets = sorted(
        {path.target_label_id for path in paths if path.target_label_id is not None}
    )
    target_label_id = request.target_label_id
    if target_label_id is None and len(path_targets) == 1:
        target_label_id = path_targets[0]
    if target_label_id is not None and target_label_id not in path_targets:
        raise ApiError(
            "MANUAL_LABEL_REBASE_TARGET_OUTSIDE_PATH",
            "目标标签不属于已发布 Mapping Bundle 的编译路径",
            409,
            details=[
                {
                    "mapping_bundle_id": bundle.mapping_bundle_id,
                    "path_target_label_ids": path_targets,
                    "target_label_id": target_label_id,
                }
            ],
        )
    if target_label_id is not None:
        _label_item(
            session,
            ctx,
            label_version_id=head.label_version_id,
            label_id=target_label_id,
        )
    preview = {
        "bundle_sha256": bundle.canonical_manifest_sha256,
        "current_release_head_generation": head.generation,
        "mapping_bundle_id": bundle.mapping_bundle_id,
        "mapping_paths": [
            {
                "comparability_status": path.comparability_status,
                "path_sha256": path.path_sha256,
                "relation_path": path.relation_path,
                "requires_recompute": path.requires_recompute,
                "target_label_id": path.target_label_id,
            }
            for path in sorted(paths, key=lambda item: item.path_sha256)
        ],
        "new_label_id": target_label_id,
        "new_label_version_id": head.label_version_id,
        "old_draft_sha256": draft.payload["draft_sha256"],
        "old_label_id": document["label_id"],
        "old_label_version_id": source_version_id,
        "requires_manual_selection": target_label_id is None,
        "schema_version": "auris.manual-label-rebase-preview/1",
    }
    return preview, target_label_id


def rebase_manual_label_draft(
    session: Session,
    ctx: RequestContext,
    *,
    audio_session_id: str,
    annotation_id: str,
    request: ManualLabelDraftRebaseRequest,
) -> dict[str, Any]:
    draft = _scoped_draft(
        session,
        ctx,
        audio_session_id=audio_session_id,
        annotation_id=annotation_id,
        for_update=request.action == "confirm",
    )
    if draft.status != "draft":
        raise ApiError("MANUAL_LABEL_REBASE_FORBIDDEN", "只有未提交 draft 可以 rebase", 409)
    head = _production_head(
        session,
        ctx,
        expected_generation=request.expected_release_head_generation,
    )
    preview, target_label_id = _rebase_preview(
        session,
        ctx,
        draft=draft,
        request=request,
        head=head,
    )
    preview_sha256 = sha256_document(preview)
    if request.action == "preview":
        return {
            "can_confirm": target_label_id is not None,
            "preview": preview,
            "preview_sha256": preview_sha256,
            "status": "preview",
            "trace_id": ctx.trace_id,
        }
    if request.preview_sha256 != preview_sha256:
        raise ApiError(
            "MANUAL_LABEL_REBASE_PREVIEW_CONFLICT",
            "Mapping diff 或 Release Head 已变化，请重新预览并二次确认",
            409,
        )
    if target_label_id is None or request.new_annotation_id is None:
        raise ApiError(
            "MANUAL_LABEL_REBASE_TARGET_REQUIRED",
            "结构性变化必须人工选择当前版本标签后才能确认 rebase",
            422,
        )
    document = draft.payload["draft_document"]
    rebased_request = ManualLabelDraftCreateRequest.model_validate(
        {
            "annotation_id": request.new_annotation_id,
            "assertion_slot": document["assertion_slot"],
            "environment": document["environment"],
            "event_or_segment_id": document["event_or_segment_id"],
            "evidence_ref": document["evidence_ref"],
            "expected_release_head_generation": head.generation,
            "label_id": target_label_id,
            "label_version_id": head.label_version_id,
            "occurred_at": document["occurred_at"],
            "subject_key": document["subject_key"],
            "subject_scope": document["subject_scope"],
            "value": document["value"],
            "value_type": document["value_type"],
        }
    )
    _version, item = _label_item(
        session,
        ctx,
        label_version_id=head.label_version_id,
        label_id=target_label_id,
    )
    if item.value_type != rebased_request.value_type:
        raise ApiError(
            "MANUAL_LABEL_REBASE_VALUE_TYPE_CHANGED",
            "目标标签 value_type 已变化，必须重新录入值而不能静默复制",
            409,
        )
    created = _persist_new_draft(
        session,
        ctx,
        audio_session_id=audio_session_id,
        request=rebased_request,
        head=head,
        provenance={
            "mapping_bundle_id": request.mapping_bundle_id,
            "old_annotation_id": annotation_id,
            "preview_sha256": preview_sha256,
        },
    )
    summary = {
        "mapping_bundle_id": request.mapping_bundle_id,
        "new_annotation_id": request.new_annotation_id,
        "old_annotation_id": annotation_id,
        "preview_sha256": preview_sha256,
        "target_label_id": target_label_id,
        "target_label_version_id": head.label_version_id,
    }
    audit = record_audit(
        session,
        ctx,
        action="manual_label_draft.rebased",
        object_type="listening_annotation",
        object_id=request.new_annotation_id,
        after=summary,
    )
    outbox = enqueue_event(
        session,
        ctx,
        event_type="manual_label_draft.rebased",
        aggregate_type="listening_annotation",
        aggregate_id=request.new_annotation_id,
        payload=summary,
    )
    session.flush()
    return {
        **created,
        **summary,
        "rebase_audit_id": audit.audit_id,
        "rebase_outbox_event_id": outbox.event_id,
        "status": "draft",
    }


def is_manual_label_draft_projection(resource: JsonResource | ListeningAnnotation) -> bool:
    payload = resource.data if isinstance(resource, JsonResource) else resource.payload
    return payload.get("annotation_kind") == "label-fact-draft"
