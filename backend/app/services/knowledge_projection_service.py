from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.models import KnowledgeEffect, KnowledgeIndex, KnowledgeQualityGate, KnowledgeSource

PROJECTION_SPECS: dict[str, tuple[type[Any], str, tuple[str, ...]]] = {
    "knowledge_sources": (
        KnowledgeSource,
        "knowledge_source_id",
        ("knowledge_source_id", "source_id", "id"),
    ),
    "knowledge_indexes": (
        KnowledgeIndex,
        "knowledge_index_id",
        ("knowledge_index_id", "index_id", "id"),
    ),
    "knowledge_quality_gates": (
        KnowledgeQualityGate,
        "knowledge_gate_id",
        ("knowledge_gate_id", "gate_id", "id"),
    ),
    "knowledge_effects": (KnowledgeEffect, "effect_id", ("effect_id", "id")),
}


def _projection_id(resource_key: str, data: dict[str, Any], aliases: tuple[str, ...]) -> str:
    for alias in aliases:
        value = data.get(alias)
        if isinstance(value, str) and value:
            return value
    return resource_key


def sync_knowledge_projection(
    session: Session,
    ctx: RequestContext,
    collection: str,
    resource_key: str,
    data: dict[str, Any],
    *,
    status: str | None = None,
    trace_id: str | None = None,
) -> object | None:
    spec = PROJECTION_SPECS.get(collection)
    if spec is None:
        return None
    model, primary_key, aliases = spec
    projection_id = _projection_id(resource_key, data, aliases)
    payload = {
        "id": projection_id,
        **data,
        primary_key: projection_id,
        "projection_source": {
            "collection": collection,
            "resource_key": resource_key,
        },
    }
    projection = session.get(model, projection_id)
    projection_status = status or data.get("status") or "draft"
    projection_trace_id = trace_id or data.get("trace_id") or ctx.trace_id
    if projection is None:
        projection = model(
            **{
                primary_key: projection_id,
                "tenant_id": ctx.tenant_id,
                "project_id": ctx.project_id,
                "status": projection_status,
                "trace_id": projection_trace_id,
                "payload": payload,
            }
        )
        session.add(projection)
    else:
        if projection.tenant_id != ctx.tenant_id or projection.project_id != ctx.project_id:
            raise ApiError(
                "PROJECTION_ID_CONFLICT",
                f"{collection} 投影 ID 已被其他租户或项目占用：{projection_id}",
                409,
            )
        projection.status = projection_status
        projection.trace_id = projection_trace_id
        projection.payload = payload
    return projection
