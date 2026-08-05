from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.core.request_identifiers import public_id_from_hex
from app.models import JsonResource, RunRecord
from app.services.audit_service import record_audit
from app.services.outbox_service import enqueue_event

_PUBLISHABLE_OUTPUT_SINK_STATES = frozenset({"active", "online", "success", "published"})
_PLATFORM_CALLBACK_TYPES = frozenset(
    {"platform_callback", "platform-callback", "external_callback"}
)


def _frozen_output_sink_refs(task_payload: dict[str, Any]) -> list[str]:
    raw_refs = task_payload.get("output_sink_refs")
    if raw_refs is None:
        return []
    if (
        not isinstance(raw_refs, list)
        or len(raw_refs) > 32
        or any(
            not isinstance(value, str) or not value.strip() or len(value.strip()) > 256
            for value in raw_refs
        )
    ):
        raise ApiError(
            "REVIEW_CALLBACK_BINDING_INVALID",
            "人审任务的平台回写绑定不合法",
            409,
        )
    return sorted({value.strip() for value in raw_refs})


def _output_sink_for_update(
    session: Session,
    ctx: RequestContext,
    output_sink_id: str,
) -> JsonResource:
    sink = session.scalar(
        select(JsonResource)
        .where(
            JsonResource.collection == "output_sinks",
            JsonResource.resource_key == output_sink_id,
            JsonResource.tenant_id == ctx.tenant_id,
            JsonResource.project_id == ctx.project_id,
        )
        .with_for_update()
    )
    if sink is None:
        raise ApiError(
            "REVIEW_CALLBACK_BINDING_NOT_FOUND",
            "人审任务绑定的平台回写目标不存在",
            409,
            details=[{"output_sink_id": output_sink_id}],
        )
    sink_type = str(sink.data.get("type") or sink.data.get("sink_type") or "").strip()
    sink_status = str(sink.status or sink.data.get("status") or "").strip()
    if sink_type not in _PLATFORM_CALLBACK_TYPES or sink_status not in (
        _PUBLISHABLE_OUTPUT_SINK_STATES
    ):
        raise ApiError(
            "REVIEW_CALLBACK_BINDING_NOT_ACTIVE",
            "人审任务绑定的平台回写目标未发布或类型不匹配",
            409,
            details=[
                {
                    "output_sink_id": output_sink_id,
                    "type": sink_type or None,
                    "status": sink_status or None,
                }
            ],
        )
    return sink


def create_review_platform_callbacks(
    session: Session,
    ctx: RequestContext,
    *,
    task_payload: dict[str, Any],
    decision_payload: dict[str, Any],
) -> list[dict[str, str]]:
    """Create callback Run/outbox pairs in the terminal decision transaction."""

    decision = str(decision_payload.get("decision") or "")
    if decision not in {"accepted", "modified", "rejected"}:
        return []
    decision_id = str(decision_payload.get("decision_id") or "").strip()
    review_task_id = str(decision_payload.get("review_task_id") or "").strip()
    root_trace_id = str(decision_payload.get("trace_id") or ctx.trace_id).strip()
    if not decision_id or not review_task_id or not root_trace_id:
        raise ApiError(
            "REVIEW_CALLBACK_DECISION_BINDING_INVALID",
            "平台回写缺少终态人审决定绑定",
            409,
        )

    affected: list[dict[str, str]] = []
    for output_sink_id in _frozen_output_sink_refs(task_payload):
        sink = _output_sink_for_update(session, ctx, output_sink_id)
        target = str(sink.data.get("target") or output_sink_id).strip()
        if not target or len(target) > 256:
            raise ApiError(
                "REVIEW_CALLBACK_TARGET_INVALID",
                "平台回写目标名称不合法",
                409,
                details=[{"output_sink_id": output_sink_id}],
            )
        identity = f"{ctx.tenant_id}\n{ctx.project_id}\n{decision_id}\n{output_sink_id}"
        run_id = public_id_from_hex(
            "callback_review",
            hashlib.sha256(identity.encode("utf-8")).hexdigest(),
            suffix_length=20,
        )
        payload_template = {
            "review_task_id": review_task_id,
            "decision_id": decision_id,
            "decision": decision,
            "evidence_pack_id": decision_payload.get("evidence_pack_id"),
            "affected_objects": decision_payload.get("affected_objects") or [],
            "root_trace_id": root_trace_id,
        }
        run_payload = {
            "run_id": run_id,
            "run_type": "external_callback",
            "status": "pending",
            "target": target,
            "output_sink_id": output_sink_id,
            "payload_template": payload_template,
            "source_review_task_id": review_task_id,
            "source_review_decision_id": decision_id,
            "root_trace_id": root_trace_id,
            "trace_id": root_trace_id,
            "run_key": f"review-callback:{decision_id}:{output_sink_id}",
            "partition_key": f"{ctx.tenant_id}/{ctx.project_id}/{output_sink_id}",
            "affected_objects": [
                {"type": "human_review_decision", "id": decision_id},
                {"type": "output_sink", "id": output_sink_id},
            ],
            "next_actions": [
                {
                    "key": "view_callback",
                    "label": "查看平台回写",
                    "route": "/api/v1/output-sinks/platform-callbacks?status=pending",
                },
                {
                    "key": "view_trace",
                    "label": "查看 Trace",
                    "route": f"/api/v1/traces/{root_trace_id}",
                },
            ],
        }
        existing = session.get(RunRecord, run_id)
        if existing is not None:
            if (
                existing.tenant_id != ctx.tenant_id
                or existing.project_id != ctx.project_id
                or existing.run_type != "external_callback"
                or existing.payload.get("source_review_decision_id") != decision_id
                or existing.payload.get("output_sink_id") != output_sink_id
            ):
                raise ApiError(
                    "REVIEW_CALLBACK_RUN_CONFLICT",
                    "既有平台回写运行与当前终态决定不一致",
                    409,
                )
            affected.append({"type": "platform_callback", "id": run_id})
            continue

        now = datetime.now(UTC)
        session.add(
            RunRecord(
                run_id=run_id,
                tenant_id=ctx.tenant_id,
                project_id=ctx.project_id,
                run_type="external_callback",
                status="pending",
                run_key=run_payload["run_key"],
                partition_key=run_payload["partition_key"],
                trace_id=root_trace_id,
                created_at=now,
                updated_at=now,
                payload=run_payload,
            )
        )
        session.flush()
        enqueue_event(
            session,
            ctx,
            event_type="external_callback.requested",
            aggregate_type="external_callback",
            aggregate_id=run_id,
            payload=run_payload,
        )
        record_audit(
            session,
            ctx,
            action="external_callback.create_from_human_review",
            object_type="external_callback",
            object_id=run_id,
            after=run_payload,
            trace_id=root_trace_id,
        )
        affected.append({"type": "platform_callback", "id": run_id})
    return affected
