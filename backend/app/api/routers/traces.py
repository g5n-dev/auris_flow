from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from sqlalchemy import or_, select

from app.api.deps import ContextDep, SessionDep
from app.core.response import envelope
from app.models import (
    AgentDecision,
    AgentRun,
    AssetLineageEdge,
    AssetMaterialization,
    AudioRecording,
    AuditLog,
    EvidencePack,
    ExternalCallbackReceipt,
    HumanReviewDecision,
    HumanReviewTask,
    ImportBatch,
    ImportBatchItem,
    JsonResource,
    KnowledgeEffect,
    KnowledgeIndex,
    KnowledgeQualityGate,
    KnowledgeSource,
    LabelCandidate,
    LabelVersion,
    ListeningAnnotation,
    OutboxDeliveryAttempt,
    OutboxEvent,
    PlatformConnection,
    PromptVersionCandidate,
    RunRecord,
    StorageObject,
    ToolCall,
    TraceRef,
    VoiceprintEnrollment,
)
from app.services.public_run_projection_service import (
    project_public_navigation_path,
    sanitize_public_run_string,
)
from app.services.read_policy_service import (
    can_read_human_review_task,
    can_read_resource_collection,
    can_read_trace_reference_collection,
    readable_resource_collections,
    require_trace_read,
    trace_reference_ids,
    trace_reference_is_visible,
)

router = APIRouter(tags=["traces"])

PUBLIC_TRACE_SCALAR_FIELDS = frozenset(
    {
        "action",
        "agent_run_id",
        "aggregate_id",
        "aggregate_type",
        "annotation_id",
        "asset_key",
        "attempt_count",
        "attempt_id",
        "attempt_number",
        "audio_intelligence_result_id",
        "audio_session_id",
        "available_at",
        "base_prompt_version",
        "candidate_id",
        "change_set_id",
        "collection",
        "completed_at",
        "correlation_id",
        "current_trace_id",
        "decision_id",
        "decision_type",
        "edge_id",
        "effect_id",
        "enrollment_id",
        "error_code",
        "event_id",
        "event_type",
        "evidence_pack_id",
        "external_record_id",
        "gate_id",
        "hit_rate",
        "id",
        "index_id",
        "import_batch_id",
        "import_item_id",
        "knowledge_effect_id",
        "knowledge_gate_id",
        "knowledge_index_id",
        "knowledge_source_id",
        "kind",
        "label_version_id",
        "lineage_source",
        "materialization_id",
        "object_id",
        "node_id",
        "source_node_id",
        "target_node_id",
        "relation",
        "queue",
        "parent_trace_id",
        "partition_key",
        "platform_connection_id",
        "processed_at",
        "ref_role",
        "ref_id",
        "ref_type",
        "request_id",
        "recording_id",
        "retry_after_seconds",
        "retryable",
        "review_task_id",
        "root_trace_id",
        "asr_result_id",
        "run_id",
        "run_type",
        "score",
        "source_asset_key",
        "source_field",
        "source_id",
        "source_run_id",
        "source_run_type",
        "source_type",
        "started_at",
        "status",
        "storage_object_version",
        "target_asset_key",
        "tool_call_id",
        "trace_ref_id",
        "voiceprint_id",
    }
)
PUBLIC_TRACE_ACTION_FIELDS = frozenset({"available_at", "key", "label", "route"})

AUDIO_INTELLIGENCE_TRACE_COLLECTIONS = frozenset(
    {
        "asr_segments",
        "audio_quality_reports",
        "speaker_turns",
        "vad_segments",
        "voiceprint_samples",
    }
)

TRACE_GRAPH_KIND_ORDER = {
    "import_batch": 20,
    "import_item": 30,
    "storage_object": 40,
    "audio_recording": 50,
    "audio_session": 60,
    "audio_intelligence_result": 80,
    "asr_result": 80,
    "evidence_pack": 90,
    "human_review_task": 100,
    "human_review_decision": 110,
    "callback_receipt": 130,
}


def _public_trace_scalar(field: str, value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return sanitize_public_run_string(value, field_name=field)


def _public_trace_span(span: dict[str, Any]) -> dict[str, Any]:
    """Project every public trace span to stable domain fields only."""
    projected: dict[str, Any] = {
        field: _public_trace_scalar(field, value)
        for field, value in span.items()
        if field in PUBLIC_TRACE_SCALAR_FIELDS
        and (value is None or isinstance(value, str | int | float | bool))
    }
    actions = span.get("next_actions")
    if isinstance(actions, list):
        projected_actions = []
        for action in actions:
            if not isinstance(action, dict):
                continue
            projected_action: dict[str, Any] = {}
            for field, value in action.items():
                if field not in PUBLIC_TRACE_ACTION_FIELDS or not (
                    value is None or isinstance(value, str | int | float | bool)
                ):
                    continue
                if field == "route" and isinstance(value, str):
                    route = project_public_navigation_path(value, field_name=field)
                    if route is not None:
                        projected_action[field] = route
                    continue
                projected_action[field] = _public_trace_scalar(field, value)
            projected_actions.append(projected_action)
        if projected_actions:
            projected["next_actions"] = projected_actions
    return projected


def _decision_task_id(decision: HumanReviewDecision) -> str | None:
    return decision.review_task_id or decision.payload.get("review_task_id")


def outbox_span(event: OutboxEvent) -> dict:
    adapter_dispatch = event.payload.get("adapter_dispatch")
    error_code = None
    retryable = None
    retry_after_seconds = event.payload.get("retry_after_seconds")
    if isinstance(adapter_dispatch, dict):
        error_code = adapter_dispatch.get("error_code")
        retryable = adapter_dispatch.get("retryable")
        retry_after_seconds = adapter_dispatch.get("retry_after_seconds", retry_after_seconds)
    if error_code is None and event.last_error and ":" in event.last_error:
        error_code = event.last_error.split(":", 1)[0]
    if retryable is None and event.last_error:
        retryable = event.status == "pending"
    next_actions = []
    if event.status == "pending" and event.last_error:
        next_actions.append(
            {
                "key": "retry_scheduled",
                "label": "等待自动重试",
                "available_at": event.available_at.isoformat() if event.available_at else None,
            }
        )
    if event.status == "dead_letter":
        next_actions.append({"key": "retry", "label": "创建重试运行"})
    if event.payload.get("trace_id"):
        next_actions.append(
            {
                "key": "view_trace",
                "label": "查看 Trace",
                "route": f"traces/{event.payload.get('trace_id')}",
            }
        )
    return {
        "kind": "outbox",
        "id": event.event_id,
        "event_type": event.event_type,
        "aggregate_type": event.aggregate_type,
        "aggregate_id": event.aggregate_id,
        "status": event.status,
        "attempt_count": event.attempt_count,
        "retryable": retryable,
        "retry_after_seconds": retry_after_seconds,
        "available_at": event.available_at.isoformat() if event.available_at else None,
        "error_code": error_code,
        "processed_at": event.processed_at.isoformat() if event.processed_at else None,
        "next_actions": next_actions,
    }


def outbox_attempt_span(attempt: OutboxDeliveryAttempt) -> dict:
    return {
        "kind": "outbox_delivery_attempt",
        "id": attempt.attempt_id,
        "attempt_id": attempt.attempt_id,
        "event_id": attempt.event_id,
        "status": attempt.status,
        "attempt_number": attempt.attempt_number,
        "error_code": attempt.error_code,
        "started_at": attempt.started_at.isoformat() if attempt.started_at else None,
        "completed_at": attempt.completed_at.isoformat() if attempt.completed_at else None,
    }


def _trace_node_id(kind: str, object_id: str) -> str:
    return f"{kind}:{object_id}"


def _run_graph_order(run_type: str) -> int:
    if run_type == "task_run":
        return 10
    if run_type == "audio_intelligence":
        return 70
    if run_type in {"external_callback", "output_sink", "platform_callback"}:
        return 120
    return 15


def _build_audio_vertical_trace_graph(
    *,
    runs: list[RunRecord],
    import_batches: list[ImportBatch],
    import_items: list[ImportBatchItem],
    storage_objects: list[StorageObject],
    recordings: list[AudioRecording],
    resources: list[JsonResource],
    evidence_packs: list[EvidencePack],
    human_review_tasks: list[HumanReviewTask],
    human_review_decisions: list[HumanReviewDecision],
    callback_receipts: list[ExternalCallbackReceipt],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build a deterministic, non-dangling business graph for one root trace."""

    nodes_by_id: dict[str, dict[str, Any]] = {}
    node_order: dict[str, int] = {}

    def add_node(
        kind: str,
        object_id: str,
        *,
        node_key: str | None = None,
        order: int | None = None,
        **fields: Any,
    ) -> str:
        node_id = _trace_node_id(kind, node_key or object_id)
        nodes_by_id[node_id] = {
            "node_id": node_id,
            "kind": kind,
            "id": object_id,
            **fields,
        }
        node_order[node_id] = order if order is not None else TRACE_GRAPH_KIND_ORDER.get(kind, 500)
        return node_id

    for run in runs:
        add_node(
            "run",
            run.run_id,
            order=_run_graph_order(run.run_type),
            run_id=run.run_id,
            run_type=run.run_type,
            status=run.status,
        )
    for batch in import_batches:
        add_node(
            "import_batch",
            batch.import_batch_id,
            import_batch_id=batch.import_batch_id,
            status=batch.status,
            root_trace_id=batch.root_trace_id,
        )
    for item in import_items:
        add_node(
            "import_item",
            item.import_item_id,
            import_item_id=item.import_item_id,
            import_batch_id=item.import_batch_id,
            external_record_id=item.external_record_id,
            audio_session_id=item.audio_session_id,
            error_code=item.error_code,
            storage_object_version=item.object_version,
            status=item.status,
        )
    for storage_object in storage_objects:
        add_node(
            "storage_object",
            storage_object.storage_object_id,
            status=storage_object.status,
            source_type=storage_object.source_type,
            source_id=storage_object.source_id,
        )
    for recording in recordings:
        add_node(
            "audio_recording",
            recording.recording_id,
            recording_id=recording.recording_id,
            audio_session_id=recording.payload.get("audio_session_id"),
            status=recording.status,
        )

    graph_resources = [
        resource
        for resource in resources
        if resource.collection == "audio_sessions"
        or resource.collection in AUDIO_INTELLIGENCE_TRACE_COLLECTIONS
    ]
    for resource in graph_resources:
        if resource.collection == "audio_sessions":
            add_node(
                "audio_session",
                resource.resource_key,
                audio_session_id=resource.resource_key,
                recording_id=resource.data.get("recording_id"),
                status=resource.status,
            )
            continue
        kind = (
            "asr_result" if resource.collection == "asr_segments" else ("audio_intelligence_result")
        )
        result_node_id = add_node(
            kind,
            resource.resource_key,
            node_key=(
                resource.resource_key
                if kind == "asr_result"
                else f"{resource.collection}:{resource.resource_key}"
            ),
            collection=resource.collection,
            audio_session_id=resource.data.get("audio_session_id"),
            recording_id=resource.data.get("recording_id"),
            source_run_id=resource.data.get("source_run_id"),
            status=resource.status,
        )
        result_id_field = (
            "asr_result_id" if kind == "asr_result" else "audio_intelligence_result_id"
        )
        nodes_by_id[result_node_id][result_id_field] = resource.resource_key
    for evidence in evidence_packs:
        add_node(
            "evidence_pack",
            evidence.evidence_pack_id,
            evidence_pack_id=evidence.evidence_pack_id,
            audio_session_id=evidence.audio_session_id,
            recording_id=evidence.recording_id,
            asr_result_id=evidence.asr_result_id,
            source_run_id=evidence.source_run_id,
            storage_object_version=evidence.storage_object_version,
            root_trace_id=evidence.root_trace_id,
            current_trace_id=evidence.current_trace_id,
            status=evidence.status,
        )
    for task in human_review_tasks:
        add_node(
            "human_review_task",
            task.review_task_id,
            review_task_id=task.review_task_id,
            audio_session_id=task.payload.get("audio_session_id"),
            evidence_pack_id=task.payload.get("evidence_pack_id"),
            queue=task.payload.get("queue"),
            status=task.status,
        )
    for decision in human_review_decisions:
        add_node(
            "human_review_decision",
            decision.decision_id,
            decision_id=decision.decision_id,
            review_task_id=_decision_task_id(decision),
            evidence_pack_id=decision.payload.get("evidence_pack_id"),
            status=decision.status,
        )
    for receipt in callback_receipts:
        add_node(
            "callback_receipt",
            receipt.callback_receipt_id,
            status=receipt.status,
            source_run_id=receipt.payload.get("run_id"),
        )

    edges_by_id: dict[str, dict[str, Any]] = {}

    def add_edge(source_node_id: str, relation: str, target_node_id: str) -> None:
        if source_node_id not in nodes_by_id or target_node_id not in nodes_by_id:
            return
        edge_id = f"{source_node_id}--{relation}--{target_node_id}"
        edges_by_id[edge_id] = {
            "edge_id": edge_id,
            "source_node_id": source_node_id,
            "target_node_id": target_node_id,
            "relation": relation,
        }

    for batch in import_batches:
        add_edge(
            _trace_node_id("run", batch.task_run_id),
            "created",
            _trace_node_id("import_batch", batch.import_batch_id),
        )
    for item in import_items:
        item_node_id = _trace_node_id("import_item", item.import_item_id)
        add_edge(
            _trace_node_id("import_batch", item.import_batch_id),
            "contains",
            item_node_id,
        )
        storage_object_id = item.payload.get("storage_object_id")
        if isinstance(storage_object_id, str) and storage_object_id:
            add_edge(
                item_node_id,
                "materialized",
                _trace_node_id("storage_object", storage_object_id),
            )
    for recording in recordings:
        recording_node_id = _trace_node_id("audio_recording", recording.recording_id)
        storage_object_id = recording.payload.get("storage_object_id")
        if isinstance(storage_object_id, str) and storage_object_id:
            add_edge(
                _trace_node_id("storage_object", storage_object_id),
                "registered_as",
                recording_node_id,
            )
        audio_session_id = recording.payload.get("audio_session_id")
        if isinstance(audio_session_id, str) and audio_session_id:
            add_edge(
                recording_node_id,
                "opened_as",
                _trace_node_id("audio_session", audio_session_id),
            )
    for run in runs:
        if run.run_type != "audio_intelligence":
            continue
        audio_session_id = run.payload.get("audio_session_id")
        if isinstance(audio_session_id, str) and audio_session_id:
            add_edge(
                _trace_node_id("audio_session", audio_session_id),
                "processed_by",
                _trace_node_id("run", run.run_id),
            )
    for resource in graph_resources:
        if resource.collection == "audio_sessions":
            continue
        kind = (
            "asr_result" if resource.collection == "asr_segments" else ("audio_intelligence_result")
        )
        source_run_id = resource.data.get("source_run_id")
        if isinstance(source_run_id, str) and source_run_id:
            add_edge(
                _trace_node_id("run", source_run_id),
                "materialized",
                _trace_node_id(
                    kind,
                    (
                        resource.resource_key
                        if kind == "asr_result"
                        else f"{resource.collection}:{resource.resource_key}"
                    ),
                ),
            )
    for evidence in evidence_packs:
        add_edge(
            _trace_node_id("asr_result", evidence.asr_result_id),
            "bound_into",
            _trace_node_id("evidence_pack", evidence.evidence_pack_id),
        )
    for task in human_review_tasks:
        evidence_pack_id = task.payload.get("evidence_pack_id")
        if isinstance(evidence_pack_id, str) and evidence_pack_id:
            add_edge(
                _trace_node_id("evidence_pack", evidence_pack_id),
                "queued_for",
                _trace_node_id("human_review_task", task.review_task_id),
            )
    decisions_by_task_id: dict[str, HumanReviewDecision] = {}
    decisions_by_evidence_id: dict[str, HumanReviewDecision] = {}
    for decision in sorted(
        human_review_decisions,
        key=lambda item: item.decision_id,
    ):
        review_task_id = _decision_task_id(decision)
        if review_task_id:
            decisions_by_task_id.setdefault(review_task_id, decision)
            add_edge(
                _trace_node_id("human_review_task", review_task_id),
                "decided_by",
                _trace_node_id("human_review_decision", decision.decision_id),
            )
        evidence_pack_id = decision.payload.get("evidence_pack_id")
        if isinstance(evidence_pack_id, str) and evidence_pack_id:
            decisions_by_evidence_id.setdefault(evidence_pack_id, decision)
    callback_run_types = {"external_callback", "output_sink", "platform_callback"}
    for run in runs:
        if run.run_type not in callback_run_types:
            continue
        decision_id = (
            run.payload.get("decision_id")
            or run.payload.get("review_decision_id")
            or run.payload.get("human_review_decision_id")
        )
        callback_decision: HumanReviewDecision | None = next(
            (item for item in human_review_decisions if item.decision_id == decision_id),
            None,
        )
        if callback_decision is None:
            review_task_id = run.payload.get("review_task_id")
            if isinstance(review_task_id, str):
                callback_decision = decisions_by_task_id.get(review_task_id)
        if callback_decision is None:
            evidence_pack_id = run.payload.get("evidence_pack_id")
            if isinstance(evidence_pack_id, str):
                callback_decision = decisions_by_evidence_id.get(evidence_pack_id)
        if callback_decision is not None:
            add_edge(
                _trace_node_id("human_review_decision", callback_decision.decision_id),
                "triggered",
                _trace_node_id("run", run.run_id),
            )
    for receipt in callback_receipts:
        source_run_id = receipt.payload.get("run_id")
        if isinstance(source_run_id, str) and source_run_id:
            add_edge(
                _trace_node_id("run", source_run_id),
                "received",
                _trace_node_id("callback_receipt", receipt.callback_receipt_id),
            )

    nodes = sorted(
        nodes_by_id.values(),
        key=lambda node: (
            node_order[node["node_id"]],
            node["node_id"],
        ),
    )
    node_position = {node["node_id"]: index for index, node in enumerate(nodes)}
    edges = sorted(
        edges_by_id.values(),
        key=lambda edge: (
            node_position[edge["source_node_id"]],
            node_position[edge["target_node_id"]],
            edge["relation"],
            edge["edge_id"],
        ),
    )
    return nodes, edges


@router.get("/traces/{trace_id}")
def get_traces_by_trace_id(
    trace_id: str,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    require_trace_read(ctx)
    scope_filter = (
        or_(
            RunRecord.trace_id == trace_id,
            RunRecord.payload["root_trace_id"].as_string() == trace_id,
        ),
        RunRecord.tenant_id == ctx.tenant_id,
        RunRecord.project_id == ctx.project_id,
    )
    runs = list(session.scalars(select(RunRecord).where(*scope_filter)))
    audits = list(
        session.scalars(
            select(AuditLog).where(
                AuditLog.trace_id == trace_id,
                AuditLog.tenant_id == ctx.tenant_id,
                AuditLog.project_id == ctx.project_id,
            )
        )
    )
    events = list(
        session.scalars(
            select(OutboxEvent).where(
                OutboxEvent.tenant_id == ctx.tenant_id,
                OutboxEvent.project_id == ctx.project_id,
                OutboxEvent.payload["trace_id"].as_string() == trace_id,
            )
        )
    )
    readable_collections = readable_resource_collections(ctx)
    resources = list(
        session.scalars(
            select(JsonResource).where(
                or_(
                    JsonResource.trace_id == trace_id,
                    JsonResource.data["root_trace_id"].as_string() == trace_id,
                ),
                JsonResource.tenant_id == ctx.tenant_id,
                JsonResource.project_id == ctx.project_id,
                JsonResource.collection.in_(readable_collections),
            )
        )
    )
    platform_connections = (
        list(
            session.scalars(
                select(PlatformConnection).where(
                    or_(
                        PlatformConnection.root_trace_id == trace_id,
                        PlatformConnection.current_trace_id == trace_id,
                    ),
                    PlatformConnection.tenant_id == ctx.tenant_id,
                    PlatformConnection.project_id == ctx.project_id,
                )
            )
        )
        if can_read_trace_reference_collection(ctx, "platform_connections")
        else []
    )
    import_batches = list(
        session.scalars(
            select(ImportBatch).where(
                or_(
                    ImportBatch.root_trace_id == trace_id,
                    ImportBatch.trace_id == trace_id,
                ),
                ImportBatch.tenant_id == ctx.tenant_id,
                ImportBatch.project_id == ctx.project_id,
            )
        )
    )
    import_batch_ids = {batch.import_batch_id for batch in import_batches}
    import_items = list(
        session.scalars(
            select(ImportBatchItem).where(
                or_(
                    ImportBatchItem.root_trace_id == trace_id,
                    ImportBatchItem.trace_id == trace_id,
                    ImportBatchItem.import_batch_id.in_(import_batch_ids),
                ),
                ImportBatchItem.tenant_id == ctx.tenant_id,
                ImportBatchItem.project_id == ctx.project_id,
            )
        )
    )
    evidence_packs = (
        list(
            session.scalars(
                select(EvidencePack).where(
                    or_(
                        EvidencePack.root_trace_id == trace_id,
                        EvidencePack.current_trace_id == trace_id,
                    ),
                    EvidencePack.tenant_id == ctx.tenant_id,
                    EvidencePack.project_id == ctx.project_id,
                )
            )
        )
        if can_read_resource_collection(ctx, "evidence_packs")
        else []
    )
    linked_recording_ids = {evidence.recording_id for evidence in evidence_packs}
    linked_recording_ids.update(
        recording_id
        for resource in resources
        if resource.collection == "audio_sessions"
        and isinstance(recording_id := resource.data.get("recording_id"), str)
        and recording_id
    )
    recordings = list(
        session.scalars(
            select(AudioRecording).where(
                or_(
                    AudioRecording.trace_id == trace_id,
                    AudioRecording.recording_id.in_(linked_recording_ids),
                ),
                AudioRecording.tenant_id == ctx.tenant_id,
                AudioRecording.project_id == ctx.project_id,
            )
        )
    )
    linked_storage_object_ids = {
        storage_object_id
        for item in import_items
        if isinstance(
            storage_object_id := item.payload.get("storage_object_id"),
            str,
        )
        and storage_object_id
    }
    linked_storage_object_ids.update(evidence.storage_object_id for evidence in evidence_packs)
    linked_storage_object_ids.update(
        storage_object_id
        for recording in recordings
        if isinstance(
            storage_object_id := recording.payload.get("storage_object_id"),
            str,
        )
        and storage_object_id
    )
    storage_objects = list(
        session.scalars(
            select(StorageObject).where(
                or_(
                    StorageObject.trace_id == trace_id,
                    StorageObject.storage_object_id.in_(linked_storage_object_ids),
                ),
                StorageObject.tenant_id == ctx.tenant_id,
                StorageObject.project_id == ctx.project_id,
            )
        )
    )
    materializations = list(
        session.scalars(
            select(AssetMaterialization).where(
                AssetMaterialization.trace_id == trace_id,
                AssetMaterialization.tenant_id == ctx.tenant_id,
                AssetMaterialization.project_id == ctx.project_id,
            )
        )
    )
    lineage_edges = list(
        session.scalars(
            select(AssetLineageEdge).where(
                AssetLineageEdge.trace_id == trace_id,
                AssetLineageEdge.tenant_id == ctx.tenant_id,
                AssetLineageEdge.project_id == ctx.project_id,
            )
        )
    )
    listening_annotations = list(
        session.scalars(
            select(ListeningAnnotation).where(
                ListeningAnnotation.trace_id == trace_id,
                ListeningAnnotation.tenant_id == ctx.tenant_id,
                ListeningAnnotation.project_id == ctx.project_id,
            )
        )
    )
    can_read_voiceprints = can_read_resource_collection(ctx, "voiceprint_enrollments")
    voiceprint_enrollments = (
        list(
            session.scalars(
                select(VoiceprintEnrollment).where(
                    VoiceprintEnrollment.trace_id == trace_id,
                    VoiceprintEnrollment.tenant_id == ctx.tenant_id,
                    VoiceprintEnrollment.project_id == ctx.project_id,
                )
            )
        )
        if can_read_voiceprints
        else []
    )
    prompt_candidates = (
        list(
            session.scalars(
                select(PromptVersionCandidate).where(
                    PromptVersionCandidate.trace_id == trace_id,
                    PromptVersionCandidate.tenant_id == ctx.tenant_id,
                    PromptVersionCandidate.project_id == ctx.project_id,
                )
            )
        )
        if can_read_resource_collection(ctx, "prompt_version_candidates")
        else []
    )
    label_versions = list(
        session.scalars(
            select(LabelVersion).where(
                LabelVersion.trace_id == trace_id,
                LabelVersion.tenant_id == ctx.tenant_id,
                LabelVersion.project_id == ctx.project_id,
            )
        )
    )
    label_candidates = list(
        session.scalars(
            select(LabelCandidate).where(
                LabelCandidate.trace_id == trace_id,
                LabelCandidate.tenant_id == ctx.tenant_id,
                LabelCandidate.project_id == ctx.project_id,
            )
        )
    )
    human_review_tasks = (
        list(
            session.scalars(
                select(HumanReviewTask).where(
                    or_(
                        HumanReviewTask.trace_id == trace_id,
                        HumanReviewTask.payload["root_trace_id"].as_string() == trace_id,
                    ),
                    HumanReviewTask.tenant_id == ctx.tenant_id,
                    HumanReviewTask.project_id == ctx.project_id,
                )
            )
        )
        if can_read_resource_collection(ctx, "human_review_tasks")
        else []
    )
    human_review_decisions = (
        list(
            session.scalars(
                select(HumanReviewDecision).where(
                    or_(
                        HumanReviewDecision.trace_id == trace_id,
                        HumanReviewDecision.payload["root_trace_id"].as_string() == trace_id,
                    ),
                    HumanReviewDecision.tenant_id == ctx.tenant_id,
                    HumanReviewDecision.project_id == ctx.project_id,
                )
            )
        )
        if can_read_resource_collection(ctx, "human_review_decisions")
        else []
    )
    knowledge_sources = list(
        session.scalars(
            select(KnowledgeSource).where(
                KnowledgeSource.trace_id == trace_id,
                KnowledgeSource.tenant_id == ctx.tenant_id,
                KnowledgeSource.project_id == ctx.project_id,
            )
        )
    )
    knowledge_indexes = list(
        session.scalars(
            select(KnowledgeIndex).where(
                KnowledgeIndex.trace_id == trace_id,
                KnowledgeIndex.tenant_id == ctx.tenant_id,
                KnowledgeIndex.project_id == ctx.project_id,
            )
        )
    )
    knowledge_quality_gates = list(
        session.scalars(
            select(KnowledgeQualityGate).where(
                KnowledgeQualityGate.trace_id == trace_id,
                KnowledgeQualityGate.tenant_id == ctx.tenant_id,
                KnowledgeQualityGate.project_id == ctx.project_id,
            )
        )
    )
    knowledge_effects = list(
        session.scalars(
            select(KnowledgeEffect).where(
                KnowledgeEffect.trace_id == trace_id,
                KnowledgeEffect.tenant_id == ctx.tenant_id,
                KnowledgeEffect.project_id == ctx.project_id,
            )
        )
    )
    agent_runs = list(
        session.scalars(
            select(AgentRun).where(
                AgentRun.trace_id == trace_id,
                AgentRun.tenant_id == ctx.tenant_id,
                AgentRun.project_id == ctx.project_id,
            )
        )
    )
    tool_calls = list(
        session.scalars(
            select(ToolCall).where(
                ToolCall.trace_id == trace_id,
                ToolCall.tenant_id == ctx.tenant_id,
                ToolCall.project_id == ctx.project_id,
            )
        )
    )
    agent_decisions = list(
        session.scalars(
            select(AgentDecision).where(
                AgentDecision.trace_id == trace_id,
                AgentDecision.tenant_id == ctx.tenant_id,
                AgentDecision.project_id == ctx.project_id,
            )
        )
    )
    trace_refs = list(
        session.scalars(
            select(TraceRef).where(
                TraceRef.trace_id == trace_id,
                TraceRef.tenant_id == ctx.tenant_id,
                TraceRef.project_id == ctx.project_id,
            )
        )
    )

    resource_review_tasks = {
        resource.resource_key: resource.data
        for resource in resources
        if resource.collection == "human_review_tasks"
    }
    resource_review_decisions = {
        resource.resource_key: resource.data
        for resource in resources
        if resource.collection == "human_review_decisions"
    }
    authorization_values: list[object] = [
        *[
            {
                "type": event.aggregate_type,
                "id": event.aggregate_id,
                "payload": event.payload,
            }
            for event in events
        ],
        *[
            {
                "type": audit.object_type,
                "id": audit.object_id,
                "before": audit.before_json,
                "after": audit.after_json,
            }
            for audit in audits
        ],
        *[run.payload for run in runs],
        *[materialization.payload for materialization in materializations],
        *[edge.payload for edge in lineage_edges],
        *[ref.payload for ref in trace_refs],
        *[agent.payload for agent in agent_runs],
        *[tool.payload for tool in tool_calls],
        *[decision.payload for decision in agent_decisions],
    ]
    referenced_review_task_ids = {
        task_id for decision in human_review_decisions if (task_id := _decision_task_id(decision))
    }
    referenced_review_task_ids.update(
        task_id
        for resource in resources
        if resource.collection == "human_review_decisions"
        and isinstance((task_id := resource.data.get("review_task_id")), str)
        and task_id
    )
    referenced_review_decision_ids: set[str] = set()
    for value in authorization_values:
        referenced_review_task_ids.update(trace_reference_ids(value, "human_review_tasks"))
        referenced_review_decision_ids.update(trace_reference_ids(value, "human_review_decisions"))

    authorization_decisions = {
        decision.decision_id: decision for decision in human_review_decisions
    }
    missing_review_decision_ids = referenced_review_decision_ids - set(authorization_decisions)
    if missing_review_decision_ids and can_read_resource_collection(ctx, "human_review_decisions"):
        linked_decisions = session.scalars(
            select(HumanReviewDecision).where(
                HumanReviewDecision.tenant_id == ctx.tenant_id,
                HumanReviewDecision.project_id == ctx.project_id,
                HumanReviewDecision.decision_id.in_(missing_review_decision_ids),
            )
        )
        authorization_decisions.update(
            {decision.decision_id: decision for decision in linked_decisions}
        )
    referenced_review_task_ids.update(
        task_id
        for decision in authorization_decisions.values()
        if (task_id := _decision_task_id(decision))
    )
    referenced_review_task_ids.update(
        task_id
        for decision in resource_review_decisions.values()
        if isinstance((task_id := decision.get("review_task_id")), str) and task_id
    )
    authorization_tasks = {task.review_task_id: task.payload for task in human_review_tasks}
    authorization_tasks.update(resource_review_tasks)
    missing_review_task_ids = referenced_review_task_ids - set(authorization_tasks)
    if missing_review_task_ids and can_read_resource_collection(ctx, "human_review_tasks"):
        linked_tasks = session.scalars(
            select(HumanReviewTask).where(
                HumanReviewTask.tenant_id == ctx.tenant_id,
                HumanReviewTask.project_id == ctx.project_id,
                HumanReviewTask.review_task_id.in_(missing_review_task_ids),
            )
        )
        authorization_tasks.update({task.review_task_id: task.payload for task in linked_tasks})

    visible_review_task_ids = {
        task_id
        for task_id, payload in authorization_tasks.items()
        if can_read_human_review_task(payload, ctx)
    }
    human_review_tasks = [
        task for task in human_review_tasks if task.review_task_id in visible_review_task_ids
    ]
    human_review_decisions = [
        decision
        for decision in human_review_decisions
        if _decision_task_id(decision) in visible_review_task_ids
    ]
    visible_review_decision_ids = {
        decision_id
        for decision_id, decision in authorization_decisions.items()
        if _decision_task_id(decision) in visible_review_task_ids
    }
    visible_review_decision_ids.update(
        decision_id
        for decision_id, decision in resource_review_decisions.items()
        if decision.get("review_task_id") in visible_review_task_ids
    )

    resources = [
        resource
        for resource in resources
        if (
            resource.collection == "human_review_tasks"
            and resource.resource_key in visible_review_task_ids
        )
        or (
            resource.collection == "human_review_decisions"
            and (
                resource.resource_key in visible_review_decision_ids
                or resource.data.get("review_task_id") in visible_review_task_ids
            )
        )
        or resource.collection not in {"human_review_tasks", "human_review_decisions"}
    ]

    def trace_value_is_visible(value: object) -> bool:
        return trace_reference_is_visible(
            value,
            ctx,
            visible_review_task_ids=visible_review_task_ids,
            visible_review_decision_ids=visible_review_decision_ids,
        )

    def event_is_visible(event: OutboxEvent) -> bool:
        return trace_value_is_visible(
            {
                "type": event.aggregate_type,
                "id": event.aggregate_id,
                "payload": event.payload,
            }
        )

    events = [event for event in events if event_is_visible(event)]
    event_ids = [event.event_id for event in events]
    delivery_attempts = (
        list(
            session.scalars(
                select(OutboxDeliveryAttempt).where(
                    OutboxDeliveryAttempt.tenant_id == ctx.tenant_id,
                    OutboxDeliveryAttempt.project_id == ctx.project_id,
                    OutboxDeliveryAttempt.event_id.in_(event_ids),
                )
            )
        )
        if event_ids
        else []
    )

    def audit_is_visible(audit: AuditLog) -> bool:
        return trace_value_is_visible(
            {
                "type": audit.object_type,
                "id": audit.object_id,
                "before": audit.before_json,
                "after": audit.after_json,
            }
        )

    audits = [audit for audit in audits if audit_is_visible(audit)]
    all_run_ids = {run.run_id for run in runs}
    callback_run_types = {"external_callback", "output_sink", "platform_callback"}
    runs = [
        run
        for run in runs
        if trace_value_is_visible(run.payload)
        and (
            run.run_type not in callback_run_types
            or trace_value_is_visible({"type": run.run_type, "id": run.run_id})
        )
    ]
    visible_run_ids = {run.run_id for run in runs}
    callback_receipts = (
        list(
            session.scalars(
                select(ExternalCallbackReceipt).where(
                    ExternalCallbackReceipt.tenant_id == ctx.tenant_id,
                    ExternalCallbackReceipt.project_id == ctx.project_id,
                    ExternalCallbackReceipt.payload["run_id"].as_string().in_(visible_run_ids),
                )
            )
        )
        if visible_run_ids and can_read_resource_collection(ctx, "work_items")
        else []
    )
    materializations = [
        materialization
        for materialization in materializations
        if trace_value_is_visible(materialization.payload)
    ]
    lineage_edges = [edge for edge in lineage_edges if trace_value_is_visible(edge.payload)]
    trace_refs = [ref for ref in trace_refs if trace_value_is_visible(ref.payload)]
    visible_agent_runs: list[tuple[AgentRun, list[dict[str, Any]]]] = []
    for agent in agent_runs:
        source_run_id = agent.payload.get("source_run_id")
        if source_run_id in all_run_ids and source_run_id not in visible_run_ids:
            continue
        if not trace_value_is_visible(agent.payload):
            continue
        raw_refs = agent.payload.get("input_refs", [])
        visible_refs = [
            ref for ref in raw_refs if isinstance(ref, dict) and trace_value_is_visible(ref)
        ]
        visible_agent_runs.append((agent, visible_refs))
    visible_agent_run_ids = {agent.agent_run_id for agent, _ in visible_agent_runs}
    tool_calls = [
        tool
        for tool in tool_calls
        if tool.payload.get("agent_run_id") in visible_agent_run_ids
        and trace_value_is_visible(tool.payload)
    ]
    agent_decisions = [
        decision
        for decision in agent_decisions
        if decision.payload.get("agent_run_id") in visible_agent_run_ids
        and trace_value_is_visible(decision.payload)
    ]
    graph_nodes, graph_edges = _build_audio_vertical_trace_graph(
        runs=runs,
        import_batches=import_batches,
        import_items=import_items,
        storage_objects=storage_objects,
        recordings=recordings,
        resources=resources,
        evidence_packs=evidence_packs,
        human_review_tasks=human_review_tasks,
        human_review_decisions=human_review_decisions,
        callback_receipts=callback_receipts,
    )
    response = envelope(
        {
            "trace_id": trace_id,
            "tenant_id": ctx.tenant_id,
            "project_id": ctx.project_id,
            "nodes": [_public_trace_span(node) for node in graph_nodes],
            "edges": [_public_trace_span(edge) for edge in graph_edges],
            "spans": [
                *[
                    {
                        "kind": "platform_connection",
                        "id": connection.platform_connection_id,
                        "platform_connection_id": connection.platform_connection_id,
                        "status": connection.status,
                        "object_id": connection.platform_connection_id,
                        "root_trace_id": connection.root_trace_id,
                        "current_trace_id": connection.current_trace_id,
                    }
                    for connection in platform_connections
                ],
                *[
                    {
                        "kind": "resource",
                        "id": resource.resource_key,
                        "collection": resource.collection,
                        "status": resource.status,
                        "object_id": resource.resource_key,
                    }
                    for resource in resources
                ],
                *[
                    {
                        "kind": "run",
                        "id": run.run_id,
                        "run_id": run.run_id,
                        "status": run.status,
                        "run_type": run.run_type,
                    }
                    for run in runs
                ],
                *[
                    {
                        "kind": "materialization",
                        "id": materialization.materialization_id,
                        "materialization_id": materialization.materialization_id,
                        "status": materialization.status,
                        "asset_key": materialization.payload.get("asset_key"),
                        "partition_key": materialization.payload.get("partition_key"),
                        "run_id": materialization.payload.get("run_id"),
                    }
                    for materialization in materializations
                ],
                *[
                    {
                        "kind": "asset_lineage_edge",
                        "id": edge.edge_id,
                        "edge_id": edge.edge_id,
                        "status": edge.status,
                        "source_asset_key": edge.payload.get("source_asset_key"),
                        "target_asset_key": edge.payload.get("target_asset_key"),
                        "materialization_id": edge.payload.get("materialization_id"),
                        "run_id": edge.payload.get("run_id"),
                        "partition_key": edge.payload.get("partition_key"),
                        "lineage_source": edge.payload.get("lineage_source"),
                    }
                    for edge in lineage_edges
                ],
                *[
                    {
                        "kind": "listening_annotation",
                        "id": annotation.annotation_id,
                        "annotation_id": annotation.annotation_id,
                        "audio_session_id": annotation.audio_session_id,
                        "status": annotation.status,
                        "object_id": annotation.annotation_id,
                    }
                    for annotation in listening_annotations
                ],
                *[
                    {
                        "kind": "voiceprint_enrollment",
                        "id": enrollment.enrollment_id,
                        "enrollment_id": enrollment.enrollment_id,
                        "voiceprint_id": enrollment.voiceprint_id,
                        "status": enrollment.status,
                        "object_id": enrollment.enrollment_id,
                    }
                    for enrollment in voiceprint_enrollments
                ],
                *[
                    {
                        "kind": "prompt_version_candidate",
                        "id": candidate.candidate_id,
                        "candidate_id": candidate.candidate_id,
                        "status": candidate.status,
                        "source_run_id": candidate.payload.get("source_run_id"),
                        "base_prompt_version": candidate.payload.get("base_prompt_version"),
                        "change_set_id": candidate.payload.get("change_set_id"),
                        "object_id": candidate.candidate_id,
                    }
                    for candidate in prompt_candidates
                ],
                *[
                    {
                        "kind": "label_version",
                        "id": label_version.label_version_id,
                        "label_version_id": label_version.label_version_id,
                        "status": label_version.status,
                        "object_id": label_version.label_version_id,
                    }
                    for label_version in label_versions
                ],
                *[
                    {
                        "kind": "label_candidate",
                        "id": candidate.candidate_id,
                        "candidate_id": candidate.candidate_id,
                        "status": candidate.status,
                        "evidence_pack_id": candidate.payload.get("evidence_pack_id"),
                        "label_version_id": candidate.payload.get("label_version_id")
                        or candidate.payload.get("label_version"),
                        "object_id": candidate.candidate_id,
                    }
                    for candidate in label_candidates
                ],
                *[
                    {
                        "kind": "human_review_task",
                        "id": task.review_task_id,
                        "review_task_id": task.review_task_id,
                        "status": task.status,
                        "queue": task.payload.get("queue"),
                        "evidence_pack_id": task.payload.get("evidence_pack_id"),
                        "object_id": task.review_task_id,
                    }
                    for task in human_review_tasks
                ],
                *[
                    {
                        "kind": "human_review_decision",
                        "id": decision.decision_id,
                        "decision_id": decision.decision_id,
                        "status": decision.status,
                        "review_task_id": decision.payload.get("review_task_id"),
                        "object_id": decision.decision_id,
                    }
                    for decision in human_review_decisions
                ],
                *[
                    {
                        "kind": "knowledge_source",
                        "id": source.knowledge_source_id,
                        "knowledge_source_id": source.knowledge_source_id,
                        "status": source.status,
                        "source_type": source.payload.get("source_type"),
                        "object_id": source.knowledge_source_id,
                    }
                    for source in knowledge_sources
                ],
                *[
                    {
                        "kind": "knowledge_index",
                        "id": index.knowledge_index_id,
                        "knowledge_index_id": index.knowledge_index_id,
                        "status": index.status,
                        "vector_collection": index.payload.get("vector_collection"),
                        "source_id": index.payload.get("source_id"),
                        "object_id": index.knowledge_index_id,
                    }
                    for index in knowledge_indexes
                ],
                *[
                    {
                        "kind": "knowledge_quality_gate",
                        "id": gate.knowledge_gate_id,
                        "knowledge_gate_id": gate.knowledge_gate_id,
                        "status": gate.status,
                        "knowledge_index_id": gate.payload.get("knowledge_index_id"),
                        "score": gate.payload.get("score"),
                        "object_id": gate.knowledge_gate_id,
                    }
                    for gate in knowledge_quality_gates
                ],
                *[
                    {
                        "kind": "knowledge_effect",
                        "id": effect.effect_id,
                        "effect_id": effect.effect_id,
                        "status": effect.status,
                        "knowledge_index_id": effect.payload.get("knowledge_index_id"),
                        "hit_rate": effect.payload.get("hit_rate"),
                        "object_id": effect.effect_id,
                    }
                    for effect in knowledge_effects
                ],
                *[
                    {
                        "kind": "agent_run",
                        "id": agent.agent_run_id,
                        "agent_run_id": agent.agent_run_id,
                        "status": agent.status,
                        "source_run_id": agent.payload.get("source_run_id"),
                        "source_run_type": agent.payload.get("source_run_type"),
                    }
                    for agent, _visible_refs in visible_agent_runs
                ],
                *[
                    {
                        "kind": "tool_call",
                        "id": tool.tool_call_id,
                        "tool_call_id": tool.tool_call_id,
                        "status": tool.status,
                        "agent_run_id": tool.payload.get("agent_run_id"),
                        "source_run_id": tool.payload.get("source_run_id"),
                    }
                    for tool in tool_calls
                ],
                *[
                    {
                        "kind": "agent_decision",
                        "id": decision.decision_id,
                        "decision_id": decision.decision_id,
                        "status": decision.status,
                        "agent_run_id": decision.payload.get("agent_run_id"),
                        "source_run_id": decision.payload.get("source_run_id"),
                        "decision_type": decision.payload.get("decision_type"),
                    }
                    for decision in agent_decisions
                ],
                *[
                    {
                        "kind": "trace_ref",
                        "id": ref.trace_ref_id,
                        "trace_ref_id": ref.trace_ref_id,
                        "status": ref.status,
                        "agent_run_id": ref.payload.get("agent_run_id"),
                        "source_run_id": ref.payload.get("source_run_id"),
                        "ref_role": ref.payload.get("ref_role"),
                        "ref_type": ref.payload.get("type"),
                        "ref_id": ref.payload.get("id"),
                        "source_field": ref.payload.get("source_field"),
                        "root_trace_id": ref.payload.get("root_trace_id"),
                        "parent_trace_id": ref.payload.get("parent_trace_id"),
                        "correlation_id": ref.payload.get("correlation_id"),
                        "request_id": ref.payload.get("request_id"),
                        "source": ref.payload.get("source"),
                    }
                    for ref in trace_refs
                ],
                *[
                    {
                        "kind": "audit",
                        "id": audit.audit_id,
                        "action": audit.action,
                        "object_id": audit.object_id,
                    }
                    for audit in audits
                ],
                *[outbox_span(event) for event in events],
                *[outbox_attempt_span(attempt) for attempt in delivery_attempts],
            ],
        },
        ctx,
    )
    response["data"]["spans"] = [_public_trace_span(span) for span in response["data"]["spans"]]
    return response
