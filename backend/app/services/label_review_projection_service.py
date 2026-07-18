from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.models import (
    HumanReviewDecision,
    HumanReviewTask,
    LabelCandidate,
    LabelTaxonomy,
    LabelVersion,
)
from app.services.label_lifecycle_compat_service import (
    LabelLifecycleDriftError,
    apply_label_version_lifecycle_fields,
    sync_label_taxonomy_projection,
)

PROJECTION_SPECS: dict[str, tuple[type[Any], str, tuple[str, ...]]] = {
    "label_versions": (LabelVersion, "label_version_id", ("label_version_id", "id")),
    "label_candidates": (LabelCandidate, "candidate_id", ("candidate_id", "id")),
    "human_review_tasks": (HumanReviewTask, "review_task_id", ("review_task_id", "id")),
    "human_review_decisions": (HumanReviewDecision, "decision_id", ("decision_id", "id")),
}


def _projection_id(resource_key: str, data: dict[str, Any], aliases: tuple[str, ...]) -> str:
    for alias in aliases:
        value = data.get(alias)
        if isinstance(value, str) and value:
            return value
    return resource_key


def sync_label_review_projection(
    session: Session,
    ctx: RequestContext,
    collection: str,
    resource_key: str,
    data: dict[str, Any],
    *,
    status: str | None = None,
    trace_id: str | None = None,
) -> object | None:
    if collection == "taxonomies":
        try:
            return sync_label_taxonomy_projection(
                session,
                ctx,
                resource_key,
                data,
                status=status,
                trace_id=trace_id,
            )
        except LabelLifecycleDriftError as exc:
            raise ApiError(
                "LABEL_TAXONOMY_STRONG_FIELD_DRIFT",
                str(exc),
                409,
            ) from exc
    spec = PROJECTION_SPECS.get(collection)
    if spec is None:
        return None
    model, primary_key, aliases = spec
    projection_id = _projection_id(resource_key, data, aliases)
    projection = session.get(model, projection_id)
    versioned = model in {LabelVersion, LabelCandidate}
    if versioned:
        if projection is None:
            incoming_version = data.get("resource_version")
            resource_version = (
                incoming_version
                if isinstance(incoming_version, int)
                and not isinstance(incoming_version, bool)
                and incoming_version >= 1
                else 1
            )
        else:
            resource_version = projection.resource_version + 1
        data["resource_version"] = resource_version
    payload = {
        "id": projection_id,
        **data,
        primary_key: projection_id,
        "projection_source": {
            "collection": collection,
            "resource_key": resource_key,
        },
    }
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
                **({"resource_version": resource_version} if versioned else {}),
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
        if versioned:
            projection.resource_version = resource_version
    if isinstance(projection, LabelVersion):
        try:
            apply_label_version_lifecycle_fields(projection, payload, conflict_policy="raise")
        except LabelLifecycleDriftError as exc:
            raise ApiError(
                "LABEL_VERSION_STRONG_FIELD_DRIFT",
                str(exc),
                409,
            ) from exc
        if projection.taxonomy_id is not None:
            taxonomy = session.get(LabelTaxonomy, projection.taxonomy_id)
            if taxonomy is None:
                taxonomy = next(
                    (
                        pending
                        for pending in session.new
                        if isinstance(pending, LabelTaxonomy)
                        and pending.taxonomy_id == projection.taxonomy_id
                    ),
                    None,
                )
            if taxonomy is None or (
                taxonomy.tenant_id != ctx.tenant_id or taxonomy.project_id != ctx.project_id
            ):
                raise ApiError(
                    "LABEL_TAXONOMY_NOT_FOUND",
                    f"标签体系不存在或不属于当前范围：{projection.taxonomy_id}",
                    409,
                )
    return projection
