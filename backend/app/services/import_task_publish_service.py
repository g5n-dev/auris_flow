from __future__ import annotations

from typing import Any

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.core.response import envelope
from app.models import JsonResource
from app.repositories.outbox_events import database_utc_now
from app.services.audit_service import record_audit
from app.services.idempotency_service import (
    replay_or_conflict,
    request_hash,
    save_idempotency_result,
)
from app.services.outbox_service import enqueue_event
from app.services.task_execution_policy import (
    AUDIO_PLATFORM_IMPORT_EXECUTION_CONTRACT,
    AUDIO_PLATFORM_IMPORT_TASK_TYPE_ID,
    freeze_import_task_version_connector,
    validate_task_version_publish_binding,
)


class AudioImportTaskPublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=1000)


async def publish_audio_import_task_version(
    session: Session,
    ctx: RequestContext,
    request: Request,
    task_version_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Freeze and activate an operational import configuration atomically.

    Model/rule releases retain their two-person release gate. An import task is
    configuration for an external data pull, and the same project-admin role is
    already authorized to trigger that production side effect, so it is
    activated synchronously to keep the data-import user story closed.
    """

    operation = f"task_versions.publish-import:{task_version_id}"
    body_hash = await request_hash(request)
    replay = replay_or_conflict(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
    )
    if replay is not None:
        return replay

    version = session.scalar(
        select(JsonResource)
        .where(
            JsonResource.tenant_id == ctx.tenant_id,
            JsonResource.project_id == ctx.project_id,
            JsonResource.collection == "task_versions",
            JsonResource.resource_key == task_version_id,
        )
        .with_for_update()
    )
    if version is None:
        raise ApiError("NOT_FOUND", f"task_versions 不存在：{task_version_id}", 404)
    task_type_id = str(version.data.get("task_type_id") or "").strip()
    if task_type_id != AUDIO_PLATFORM_IMPORT_TASK_TYPE_ID:
        raise ApiError(
            "TASK_IMPORT_TYPE_REQUIRED",
            "该发布入口仅适用于平台音频导入任务",
            409,
        )
    current_status = str(version.data.get("status") or version.status or "")
    if current_status == "published":
        raise ApiError(
            "TASK_VERSION_ALREADY_PUBLISHED",
            "任务版本已经发布",
            409,
            details=[{"task_version_id": task_version_id}],
        )
    if current_status != "draft":
        raise ApiError(
            "TASK_VERSION_NOT_DRAFT",
            "只有草稿状态的平台音频导入任务可以发布",
            409,
            details=[{"task_version_id": task_version_id, "status": current_status}],
        )

    before = dict(version.data)
    frozen = freeze_import_task_version_connector(session, ctx, version.data)
    validate_task_version_publish_binding(
        session,
        ctx,
        frozen,
        task_version_id=task_version_id,
    )
    published_at = database_utc_now(session).isoformat()
    published = {
        **frozen,
        "status": "published",
        "published_at": published_at,
        "published_by": ctx.user_id,
        "publish_reason": payload.get("reason"),
        "trace_id": ctx.trace_id,
    }
    version.data = published
    version.status = "published"
    version.trace_id = ctx.trace_id
    session.flush()

    record_audit(
        session,
        ctx,
        action="task_versions.publish",
        object_type="task_versions",
        object_id=task_version_id,
        before=before,
        after=published,
    )
    enqueue_event(
        session,
        ctx,
        event_type="task_version.published",
        aggregate_type="task_version",
        aggregate_id=task_version_id,
        payload={
            "task_version_id": task_version_id,
            "task_type_id": AUDIO_PLATFORM_IMPORT_TASK_TYPE_ID,
            "status": "published",
            "connector_id": published.get("connector_id"),
            "connector_snapshot_sha256": published.get("connector_snapshot_sha256"),
            "execution_contract": AUDIO_PLATFORM_IMPORT_EXECUTION_CONTRACT,
            "published_at": published_at,
            "published_by": ctx.user_id,
        },
    )
    response = envelope(published, ctx)
    save_idempotency_result(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=202,
        response_json=response,
    )
    session.commit()
    return response
