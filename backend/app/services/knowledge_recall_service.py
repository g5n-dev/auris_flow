from __future__ import annotations

import os
from typing import Any
from urllib.error import HTTPError, URLError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.embeddings import EMBEDDING_SPACE_FINGERPRINT_FIELD, EmbeddingConfigurationError
from app.core.errors import ApiError
from app.models import RunRecord
from app.services.adapters import (
    QDRANT_AUTHORIZED_POINT_IDS_FIELD,
    configured_real_qdrant_client,
    configured_real_qdrant_embedding_space_fingerprint,
    real_qdrant_filter_reference,
    validate_real_qdrant_authorized_point_ids,
)

QDRANT_RECORDED_AUTHORITY_FIELDS = (
    "tenant_id",
    "project_id",
    "trace_id",
    "collection",
    "knowledge_index_id",
    "knowledge_source_id",
    "source_id",
    "source_type",
    "asset_key",
    "version",
    "business_ref",
    EMBEDDING_SPACE_FINGERPRINT_FIELD,
)
QDRANT_CURRENT_AUTHORITY_FIELDS = tuple(
    field for field in QDRANT_RECORDED_AUTHORITY_FIELDS if field != "trace_id"
)
QDRANT_SQL_AUTHORITY_FIELDS = tuple(
    field for field in QDRANT_CURRENT_AUTHORITY_FIELDS if field != "business_ref"
)


def recall_knowledge_index(
    session: Session,
    ctx: RequestContext,
    *,
    knowledge_index_id: str,
    qdrant_payload: dict[str, Any],
    query: str,
    top_k: int,
) -> dict[str, Any]:
    if os.environ.get("AURIS_QDRANT_ADAPTER", "").lower() == "real":
        try:
            current_fingerprint = configured_real_qdrant_embedding_space_fingerprint()
        except (EmbeddingConfigurationError, ValueError) as exc:
            raise ApiError(
                "KNOWLEDGE_RECALL_CONFIGURATION_INVALID",
                "知识召回向量空间配置无效",
                503,
                details=[{"code": exc.__class__.__name__}],
                retryable=False,
            ) from exc
        current_payload = {
            **qdrant_payload,
            EMBEDDING_SPACE_FINGERPRINT_FIELD: current_fingerprint,
        }
        authoritative_points = qdrant_dispatch_authority(
            session,
            ctx,
            knowledge_index_id=knowledge_index_id,
            current_payload=current_payload,
        )
        collection = str(current_payload["collection"])
        if not authoritative_points:
            return recall_payload(
                ctx,
                knowledge_index_id=knowledge_index_id,
                query=query,
                top_k=top_k,
                hits=[],
                mode="real_qdrant_authority_empty",
                collection=collection,
                filter_ref=real_qdrant_filter_reference(
                    current_payload,
                    authorized_point_count=0,
                ),
            )
        search_payload = {
            **current_payload,
            QDRANT_AUTHORIZED_POINT_IDS_FIELD: sorted(authoritative_points),
        }
        raw = recall_from_real_qdrant(search_payload, query=query, top_k=top_k)
        points = raw.get("points") if isinstance(raw, dict) else None
        if not isinstance(points, list):
            raise ApiError(
                "KNOWLEDGE_RECALL_RESPONSE_INVALID",
                "知识召回响应格式无效",
                502,
                retryable=True,
            )
        scope_violations = [
            violation
            for point in points
            if (
                violation := qdrant_point_integrity_violation(
                    point,
                    ctx=ctx,
                    knowledge_index_id=knowledge_index_id,
                    collection=collection,
                    authoritative_points=authoritative_points,
                    current_payload=current_payload,
                )
            )
        ]
        if scope_violations:
            raise ApiError(
                "KNOWLEDGE_RECALL_SCOPE_VIOLATION",
                "知识召回结果超出当前租户或项目范围",
                502,
                details=scope_violations,
                retryable=False,
            )
        raw_hits = [
            business_hit_from_payload(
                authoritative_points.get(str(point.get("id") or ""), {}),
                point_id=str(point.get("id", "")) if isinstance(point, dict) else "",
                score=point.get("score") if isinstance(point, dict) else None,
                rank=index + 1,
            )
            for index, point in enumerate(points)
        ]
        hits = [hit for hit in raw_hits if hit]
        return recall_payload(
            ctx,
            knowledge_index_id=knowledge_index_id,
            query=query,
            top_k=top_k,
            hits=hits,
            mode=raw["mode"],
            collection=raw["collection"],
            filter_ref=real_qdrant_filter_reference(
                current_payload,
                authorized_point_count=len(authoritative_points),
            ),
        )

    hits = recall_from_local_dispatches(
        session,
        ctx,
        knowledge_index_id=knowledge_index_id,
        qdrant_payload=qdrant_payload,
        top_k=top_k,
    )
    return recall_payload(
        ctx,
        knowledge_index_id=knowledge_index_id,
        query=query,
        top_k=top_k,
        hits=hits,
        mode="local_dispatch_receipts",
        collection=str(qdrant_payload["collection"]),
        filter_ref={
            "tenant_id": ctx.tenant_id,
            "project_id": ctx.project_id,
            "knowledge_index_id": knowledge_index_id,
        },
    )


def qdrant_dispatch_authority(
    session: Session,
    ctx: RequestContext,
    *,
    knowledge_index_id: str,
    current_payload: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    dispatch_json = RunRecord.payload["dispatch"]
    details_json = dispatch_json["details"]
    payload_json = details_json["qdrant_payload"]
    statement = select(RunRecord).where(
        RunRecord.tenant_id == ctx.tenant_id,
        RunRecord.project_id == ctx.project_id,
        RunRecord.status == "success",
        RunRecord.run_type.in_(("knowledge_build", "knowledge_sync")),
        dispatch_json["adapter"].as_string() == "qdrant",
        details_json["mode"].as_string() == "real",
    )
    for field in QDRANT_SQL_AUTHORITY_FIELDS:
        expected = current_payload.get(field)
        if expected is None:
            return {}
        statement = statement.where(payload_json[field].as_string() == str(expected))
    records = session.scalars(
        statement.order_by(RunRecord.updated_at.desc(), RunRecord.run_id.desc()).limit(500)
    )
    for record in records:
        dispatch = record.payload.get("dispatch") if isinstance(record.payload, dict) else None
        if not isinstance(dispatch, dict) or dispatch.get("adapter") != "qdrant":
            continue
        details = dispatch.get("details")
        if not isinstance(details, dict) or details.get("mode") != "real":
            continue
        payload = details.get("qdrant_payload")
        point_ids = details.get("point_ids")
        if not isinstance(payload, dict) or not isinstance(point_ids, list):
            continue
        if details.get(EMBEDDING_SPACE_FINGERPRINT_FIELD) != payload.get(
            EMBEDDING_SPACE_FINGERPRINT_FIELD
        ) or payload.get(EMBEDDING_SPACE_FINGERPRINT_FIELD) != current_payload.get(
            EMBEDDING_SPACE_FINGERPRINT_FIELD
        ):
            continue
        if payload.get("knowledge_index_id") != knowledge_index_id:
            continue
        if payload.get("tenant_id") != ctx.tenant_id or payload.get("project_id") != ctx.project_id:
            continue
        if _authority_mismatches(
            payload,
            current_payload,
            fields=QDRANT_CURRENT_AUTHORITY_FIELDS,
        ):
            continue
        try:
            normalized_point_ids = validate_real_qdrant_authorized_point_ids(
                {QDRANT_AUTHORIZED_POINT_IDS_FIELD: point_ids}
            )
        except ValueError:
            continue
        if not normalized_point_ids:
            continue
        return {point_id: payload for point_id in normalized_point_ids}
    return {}


def recall_from_real_qdrant(
    qdrant_payload: dict[str, Any], *, query: str, top_k: int
) -> dict[str, Any]:
    try:
        return configured_real_qdrant_client().search_index_payload(
            qdrant_payload, query=query, top_k=top_k
        )
    except (OSError, URLError, HTTPError, TimeoutError, ValueError) as exc:
        raise ApiError(
            "KNOWLEDGE_RECALL_FAILED",
            "知识召回查询失败",
            502,
            details=[{"code": exc.__class__.__name__}],
            retryable=True,
        ) from exc


def recall_from_local_dispatches(
    session: Session,
    ctx: RequestContext,
    *,
    knowledge_index_id: str,
    qdrant_payload: dict[str, Any],
    top_k: int,
) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    records = session.scalars(
        select(RunRecord)
        .where(
            RunRecord.tenant_id == ctx.tenant_id,
            RunRecord.project_id == ctx.project_id,
            RunRecord.status == "success",
            RunRecord.run_type.in_(("knowledge_build", "knowledge_sync")),
        )
        .order_by(RunRecord.updated_at.desc(), RunRecord.run_id.desc())
        .limit(200)
    )
    for record in records:
        dispatch = record.payload.get("dispatch") if isinstance(record.payload, dict) else None
        if not isinstance(dispatch, dict) or dispatch.get("adapter") != "qdrant":
            continue
        details = dispatch.get("details")
        if not isinstance(details, dict):
            continue
        payload = details.get("qdrant_payload")
        if not isinstance(payload, dict):
            continue
        if payload.get("collection") != qdrant_payload.get("collection"):
            continue
        if payload.get("tenant_id") != ctx.tenant_id or payload.get("project_id") != ctx.project_id:
            continue
        if payload.get("knowledge_index_id") != knowledge_index_id:
            continue
        if _authority_mismatches(
            payload,
            qdrant_payload,
            fields=QDRANT_CURRENT_AUTHORITY_FIELDS,
        ):
            continue
        point_ids = details.get("point_ids") if isinstance(details.get("point_ids"), list) else []
        hit = business_hit_from_payload(
            payload,
            point_id=str(point_ids[0]) if point_ids else "",
            score=1.0 - (len(hits) * 0.01),
            rank=len(hits) + 1,
        )
        if hit:
            hits.append(hit)
        if len(hits) >= top_k:
            break
    return hits


def business_hit_from_payload(
    payload: Any, *, point_id: str, score: Any, rank: int
) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    business_ref = payload.get("business_ref")
    return {
        "rank": rank,
        "score": float(score) if isinstance(score, (int, float)) else None,
        "point_id": point_id,
        "collection": payload.get("collection"),
        "knowledge_source_id": payload.get("knowledge_source_id"),
        "knowledge_index_id": payload.get("knowledge_index_id"),
        "source_id": payload.get("source_id"),
        "source_type": payload.get("source_type"),
        "asset_key": payload.get("asset_key"),
        "version": payload.get("version"),
        "business_ref": business_ref if isinstance(business_ref, dict) else {},
        "trace_id": payload.get("trace_id"),
        "evidence_ref": {
            "asset_key": payload.get("asset_key"),
            "trace_id": payload.get("trace_id"),
        },
    }


def qdrant_point_integrity_violation(
    point: Any,
    *,
    ctx: RequestContext,
    knowledge_index_id: str,
    collection: str,
    authoritative_points: dict[str, dict[str, Any]],
    current_payload: dict[str, Any],
) -> dict[str, Any] | None:
    if not isinstance(point, dict) or not isinstance(point.get("payload"), dict):
        return {"code": "POINT_PAYLOAD_INVALID"}
    payload = point["payload"]
    expected = {
        "tenant_id": ctx.tenant_id,
        "project_id": ctx.project_id,
        "knowledge_index_id": knowledge_index_id,
        "collection": collection,
    }
    mismatches = {
        key: {"expected": expected_value, "actual": payload.get(key)}
        for key, expected_value in expected.items()
        if payload.get(key) != expected_value
    }
    point_id = str(point.get("id") or "")
    if mismatches:
        return {
            "code": "POINT_SCOPE_MISMATCH",
            "point_id": point_id,
            "fields": sorted(mismatches),
        }
    recorded_payload = authoritative_points.get(point_id)
    if recorded_payload is None:
        return {"code": "POINT_NOT_IN_DISPATCH_LEDGER", "point_id": point_id}
    recorded_mismatches = _authority_mismatches(
        payload,
        recorded_payload,
        fields=QDRANT_RECORDED_AUTHORITY_FIELDS,
    )
    if recorded_mismatches:
        return {
            "code": "POINT_PAYLOAD_TAMPERED",
            "point_id": point_id,
            "fields": recorded_mismatches,
        }
    stale_fields = _authority_mismatches(
        recorded_payload,
        current_payload,
        fields=QDRANT_CURRENT_AUTHORITY_FIELDS,
    )
    if stale_fields:
        return {
            "code": "POINT_VERSION_STALE",
            "point_id": point_id,
            "fields": stale_fields,
        }
    return None


def _authority_mismatches(
    actual: dict[str, Any],
    expected: dict[str, Any],
    *,
    fields: tuple[str, ...],
) -> list[str]:
    return sorted(field for field in fields if actual.get(field) != expected.get(field))


def recall_payload(
    ctx: RequestContext,
    *,
    knowledge_index_id: str,
    query: str,
    top_k: int,
    hits: list[dict[str, Any]],
    mode: str,
    collection: str,
    filter_ref: dict[str, Any],
) -> dict[str, Any]:
    return {
        "knowledge_index_id": knowledge_index_id,
        "query": query,
        "top_k": top_k,
        "mode": mode,
        "collection": collection,
        "filter": filter_ref,
        "hits": hits,
        "hit_count": len(hits),
        "recall_trace_id": ctx.trace_id,
    }
