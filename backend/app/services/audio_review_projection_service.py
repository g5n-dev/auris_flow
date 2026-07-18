from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.models import ListeningAnnotation, VoiceprintEnrollment


def persist_listening_annotation_projection(
    session: Session,
    ctx: RequestContext,
    *,
    annotation_id: str,
    audio_session_id: str,
    status: str,
    payload: dict[str, Any],
) -> ListeningAnnotation:
    projection = session.scalar(
        select(ListeningAnnotation).where(
            ListeningAnnotation.annotation_id == annotation_id,
            ListeningAnnotation.tenant_id == ctx.tenant_id,
            ListeningAnnotation.project_id == ctx.project_id,
        )
    )
    if projection is None:
        projection = ListeningAnnotation(
            annotation_id=annotation_id,
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            audio_session_id=audio_session_id,
            status=status,
            trace_id=ctx.trace_id,
            payload=payload,
        )
        session.add(projection)
    else:
        projection.audio_session_id = audio_session_id
        projection.status = status
        projection.trace_id = ctx.trace_id
        projection.payload = payload
    return projection


def persist_voiceprint_enrollment_projection(
    session: Session,
    ctx: RequestContext,
    *,
    enrollment_id: str,
    voiceprint_id: str,
    status: str,
    payload: dict[str, Any],
) -> VoiceprintEnrollment:
    projection = session.scalar(
        select(VoiceprintEnrollment).where(
            VoiceprintEnrollment.enrollment_id == enrollment_id,
            VoiceprintEnrollment.tenant_id == ctx.tenant_id,
            VoiceprintEnrollment.project_id == ctx.project_id,
        )
    )
    if projection is None:
        projection = VoiceprintEnrollment(
            enrollment_id=enrollment_id,
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            voiceprint_id=voiceprint_id,
            status=status,
            trace_id=ctx.trace_id,
            payload=payload,
        )
        session.add(projection)
    else:
        projection.voiceprint_id = voiceprint_id
        projection.status = status
        projection.trace_id = ctx.trace_id
        projection.payload = payload
    return projection
