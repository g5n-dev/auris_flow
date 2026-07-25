"""Authoritative, scope-bound options for the browser workspace shell."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.models import (
    JsonResource,
    LabelVersion,
    Project,
    ProjectSceneProfileBinding,
    Tenant,
)

_ISO_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}")
_CONTEXT_COLLECTIONS = frozenset(
    {
        "stores",
        "task_versions",
        "label_versions",
        "audio_sessions",
        "recordings",
    }
)


def _string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _business_dates(resources: list[JsonResource]) -> list[str]:
    dates: set[str] = set()
    for resource in resources:
        payload = resource.data if isinstance(resource.data, dict) else {}
        for key in (
            "business_date",
            "started_at",
            "recorded_at",
            "occurred_at",
            "created_at",
            "date",
        ):
            value = _string(payload.get(key))
            if value and _ISO_DATE_PREFIX.match(value):
                dates.add(value[:10])
    return sorted(dates, reverse=True)


def _store_options(resources: list[JsonResource]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for resource in resources:
        if resource.collection != "stores":
            continue
        payload = resource.data if isinstance(resource.data, dict) else {}
        store_id = _string(payload.get("store_id")) or resource.resource_key
        rows.append(
            {
                "store_id": store_id,
                "name": _string(payload.get("name")) or store_id,
                "status": resource.status or _string(payload.get("status")) or "unknown",
            }
        )
    return sorted(rows, key=lambda item: str(item["store_id"]))


def _model_version_options(resources: list[JsonResource]) -> list[dict[str, object]]:
    versions: dict[str, dict[str, object]] = {}
    for resource in resources:
        if resource.collection != "task_versions":
            continue
        payload = resource.data if isinstance(resource.data, dict) else {}
        version_id = _string(payload.get("model_version"))
        if not version_id:
            continue
        option = versions.setdefault(
            version_id,
            {
                "id": version_id,
                "label": version_id,
                "status": resource.status or _string(payload.get("status")) or "unknown",
                "source_task_version_ids": [],
            },
        )
        source_ids = option["source_task_version_ids"]
        if isinstance(source_ids, list) and resource.resource_key not in source_ids:
            source_ids.append(resource.resource_key)
    return sorted(versions.values(), key=lambda item: str(item["id"]))


def _label_version_options(
    session: Session,
    ctx: RequestContext,
    resources: list[JsonResource],
) -> list[dict[str, object]]:
    versions: dict[str, dict[str, object]] = {}
    for resource in resources:
        if resource.collection != "label_versions":
            continue
        payload = resource.data if isinstance(resource.data, dict) else {}
        version_id = _string(payload.get("label_version_id")) or resource.resource_key
        versions[version_id] = {
            "id": version_id,
            "label": _string(payload.get("version"))
            or _string(payload.get("semantic_version"))
            or version_id,
            "status": resource.status or _string(payload.get("status")) or "unknown",
        }
    strong_rows = session.scalars(
        select(LabelVersion)
        .where(
            LabelVersion.tenant_id == ctx.tenant_id,
            LabelVersion.project_id == ctx.project_id,
        )
        .order_by(LabelVersion.label_version_id)
    )
    for row in strong_rows:
        versions.setdefault(
            row.label_version_id,
            {
                "id": row.label_version_id,
                "label": row.semantic_version or row.label_version_id,
                "status": row.artifact_status or row.status,
            },
        )
    return sorted(versions.values(), key=lambda item: str(item["id"]))


def _active_scene_binding(
    session: Session,
    ctx: RequestContext,
) -> dict[str, object] | None:
    binding = session.scalar(
        select(ProjectSceneProfileBinding).where(
            ProjectSceneProfileBinding.tenant_id == ctx.tenant_id,
            ProjectSceneProfileBinding.project_id == ctx.project_id,
            ProjectSceneProfileBinding.environment == "production",
            ProjectSceneProfileBinding.status == "active",
        )
    )
    if binding is None:
        return None
    return {
        "binding_id": binding.binding_id,
        "environment": binding.environment,
        "scene_profile_id": binding.scene_profile_id,
        "scene_profile_version_id": binding.scene_profile_version_id,
        "manifest_sha256": binding.manifest_sha256,
        "status": binding.status,
        "resource_version": binding.resource_version,
        "trace_id": binding.trace_id,
    }


def get_workspace_context_options(
    session: Session,
    ctx: RequestContext,
) -> dict[str, Any]:
    tenant = session.get(Tenant, ctx.tenant_id)
    project = session.get(Project, ctx.project_id)
    if (
        tenant is None
        or tenant.status != "active"
        or project is None
        or project.tenant_id != ctx.tenant_id
        or project.status != "active"
    ):
        raise ApiError("AUTH_SCOPE_REJECTED", "请求资源不可用", 404)

    resources = list(
        session.scalars(
            select(JsonResource)
            .where(
                JsonResource.tenant_id == ctx.tenant_id,
                JsonResource.project_id == ctx.project_id,
                JsonResource.collection.in_(_CONTEXT_COLLECTIONS),
            )
            .order_by(JsonResource.collection, JsonResource.resource_key)
            .limit(1000)
        )
    )
    stores = _store_options(resources)
    dates = _business_dates(resources)
    model_versions = _model_version_options(resources)
    label_versions = _label_version_options(session, ctx, resources)
    project_data = project.data if isinstance(project.data, dict) else {}
    store_ids = {str(item["store_id"]) for item in stores}
    model_ids = {str(item["id"]) for item in model_versions}
    label_ids = {str(item["id"]) for item in label_versions}

    requested_store = _string(project_data.get("store_id"))
    requested_date = _string(project_data.get("business_date"))
    requested_model = _string(project_data.get("model_version"))
    requested_label = _string(project_data.get("label_version"))
    defaults = {
        "store_id": (
            requested_store
            if requested_store in store_ids
            else str(stores[0]["store_id"])
            if stores
            else None
        ),
        "business_date": (
            requested_date if requested_date in dates else dates[0] if dates else None
        ),
        "model_version": (
            requested_model
            if requested_model in model_ids
            else str(model_versions[0]["id"])
            if model_versions
            else None
        ),
        "label_version": (
            requested_label
            if requested_label in label_ids
            else str(label_versions[0]["id"])
            if label_versions
            else None
        ),
    }
    return {
        "scope": {
            "tenant_id": tenant.tenant_id,
            "tenant_name": tenant.name,
            "project_id": project.project_id,
            "project_name": project.name,
        },
        "stores": stores,
        "business_dates": dates,
        "model_versions": model_versions,
        "label_versions": label_versions,
        "defaults": defaults,
        "active_scene_binding": _active_scene_binding(session, ctx),
        "as_of": datetime.now(UTC).isoformat(),
        "trace_id": ctx.trace_id,
    }
