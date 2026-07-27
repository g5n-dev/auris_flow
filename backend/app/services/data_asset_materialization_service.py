from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.core.request_identifiers import public_id_from_hex
from app.models import (
    AssetLineageEdge,
    AssetMaterialization,
    AssetPartition,
    DataAsset,
    JsonResource,
    RunRecord,
    StorageObject,
)

ASSET_STORAGE_READY_STATUSES = frozenset({"verified"})
ASSET_STORAGE_PROVIDERS = frozenset({"minio", "s3", "obs", "oss"})
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MAX_MATERIALIZATION_STORAGE_OBJECTS = 16


def _scoped_id(prefix: str, tenant_id: str, project_id: str, key: str) -> str:
    digest = hashlib.sha256(f"{tenant_id}|{project_id}|{key}".encode()).hexdigest()
    return public_id_from_hex(prefix, digest, suffix_length=20)


def data_asset_id(ctx: RequestContext, asset_key: str) -> str:
    return _scoped_id("asset", ctx.tenant_id, ctx.project_id, asset_key)


def partition_id(ctx: RequestContext, asset_key: str, partition_key: str) -> str:
    return _scoped_id("partition", ctx.tenant_id, ctx.project_id, f"{asset_key}|{partition_key}")


def materialization_id_for(
    ctx: RequestContext, asset_key: str, partition_key: str, run_id: str
) -> str:
    return _scoped_id("mat", ctx.tenant_id, ctx.project_id, f"{asset_key}|{partition_key}|{run_id}")


def lineage_edge_id_for(
    ctx: RequestContext,
    source_asset_key: str,
    target_asset_key: str,
    *,
    materialization_id: str | None = None,
) -> str:
    scope = materialization_id or "static"
    return _scoped_id(
        "lineage",
        ctx.tenant_id,
        ctx.project_id,
        f"{source_asset_key}|{target_asset_key}|{scope}",
    )


def _asset_storage_error(
    code: str,
    message: str,
    record: RunRecord,
    *,
    storage_object_id: str | None = None,
    status_code: int = 409,
    extra: dict[str, Any] | None = None,
) -> ApiError:
    return ApiError(
        code,
        message,
        status_code,
        retryable=False,
        details=[
            {
                "run_id": record.run_id,
                "trace_id": record.trace_id,
                **(
                    {"storage_object_id": storage_object_id}
                    if storage_object_id is not None
                    else {}
                ),
                **(extra or {}),
            }
        ],
    )


def _completion_storage_object_ids(
    record: RunRecord,
    result_ref: dict[str, Any],
) -> list[str]:
    for field in ("object_uri", "uri", "storage_uri", "download_url"):
        if result_ref.get(field):
            raise _asset_storage_error(
                "ASSET_MATERIALIZATION_STORAGE_REFERENCE_UNSAFE",
                "数据资产物化禁止使用 URI 或本地路径代替已验证的对象记录",
                record,
                extra={"field": field},
            )
    if result_ref.get("storage_objects") is not None:
        raise _asset_storage_error(
            "ASSET_MATERIALIZATION_STORAGE_DESCRIPTORS_FORBIDDEN",
            "数据资产完成回执只能引用已登记对象，不能携带自声明对象描述符",
            record,
            extra={"field": "storage_objects"},
        )

    raw_ids: list[Any] = []
    direct_id = result_ref.get("storage_object_id")
    if direct_id is not None:
        raw_ids.append(direct_id)
    listed_ids = result_ref.get("storage_object_ids")
    if listed_ids is not None:
        if not isinstance(listed_ids, list):
            raise _asset_storage_error(
                "ASSET_MATERIALIZATION_STORAGE_REFERENCE_INVALID",
                "storage_object_ids 必须是对象 ID 列表",
                record,
                status_code=422,
            )
        raw_ids.extend(listed_ids)

    raw_refs = result_ref.get("storage_refs")
    if raw_refs is not None:
        if not isinstance(raw_refs, list):
            raise _asset_storage_error(
                "ASSET_MATERIALIZATION_STORAGE_REFERENCE_INVALID",
                "storage_refs 必须是已登记对象引用列表",
                record,
                status_code=422,
            )
        for index, raw_ref in enumerate(raw_refs):
            if not isinstance(raw_ref, dict):
                raise _asset_storage_error(
                    "ASSET_MATERIALIZATION_STORAGE_REFERENCE_INVALID",
                    "storage_refs 中的每一项都必须是对象引用",
                    record,
                    status_code=422,
                    extra={"index": index},
                )
            if any(raw_ref.get(field) for field in ("uri", "object_uri", "local_path")):
                raise _asset_storage_error(
                    "ASSET_MATERIALIZATION_STORAGE_REFERENCE_UNSAFE",
                    "数据资产物化禁止使用 URI 或本地路径代替已验证的对象记录",
                    record,
                    extra={"index": index},
                )
            unexpected = set(raw_ref) - {"kind", "storage_object_id"}
            if unexpected:
                raise _asset_storage_error(
                    "ASSET_MATERIALIZATION_STORAGE_REFERENCE_INVALID",
                    "storage_refs 不能覆盖已登记对象的 Provider、路径或校验元数据",
                    record,
                    status_code=422,
                    extra={"index": index, "unexpected_fields": sorted(unexpected)},
                )
            if raw_ref.get("kind") not in {None, "storage_object"}:
                raise _asset_storage_error(
                    "ASSET_MATERIALIZATION_STORAGE_REFERENCE_INVALID",
                    "storage_refs 仅允许 storage_object 类型",
                    record,
                    status_code=422,
                    extra={"index": index},
                )
            raw_ids.append(raw_ref.get("storage_object_id"))

    normalized: list[str] = []
    for raw_id in raw_ids:
        if not isinstance(raw_id, str) or not raw_id.strip():
            raise _asset_storage_error(
                "ASSET_MATERIALIZATION_STORAGE_REFERENCE_INVALID",
                "对象存储引用必须包含非空 storage_object_id",
                record,
                status_code=422,
            )
        storage_object_id = raw_id.strip()
        if storage_object_id not in normalized:
            normalized.append(storage_object_id)
    if not normalized:
        raise _asset_storage_error(
            "ASSET_MATERIALIZATION_STORAGE_REFERENCE_REQUIRED",
            "数据资产物化完成回执必须引用已验证的对象存储记录",
            record,
            status_code=422,
        )
    if len(normalized) > MAX_MATERIALIZATION_STORAGE_OBJECTS:
        raise _asset_storage_error(
            "ASSET_MATERIALIZATION_STORAGE_REFERENCE_INVALID",
            f"单次物化最多引用 {MAX_MATERIALIZATION_STORAGE_OBJECTS} 个对象",
            record,
            status_code=422,
        )
    return normalized


def _verified_materialization_storage_refs(
    session: Session,
    ctx: RequestContext,
    record: RunRecord,
    result_ref: dict[str, Any],
) -> list[dict[str, Any]]:
    if record.tenant_id != ctx.tenant_id or record.project_id != ctx.project_id:
        raise _asset_storage_error(
            "ASSET_MATERIALIZATION_RUN_SCOPE_FORBIDDEN",
            "数据资产物化运行不属于当前租户和项目",
            record,
            status_code=403,
        )
    expected_prefix = (
        f"tenants/{record.tenant_id}/projects/{record.project_id}/runs/{record.run_id}/"
    )
    accepted_trace_ids = {
        str(value)
        for value in (
            record.trace_id,
            (record.payload.get("impact_scope") or {}).get("root_trace_id")
            if isinstance(record.payload.get("impact_scope"), dict)
            else None,
        )
        if value
    }
    refs: list[dict[str, Any]] = []
    for storage_object_id in _completion_storage_object_ids(record, result_ref):
        storage_object = session.scalar(
            select(StorageObject)
            .where(StorageObject.storage_object_id == storage_object_id)
            .with_for_update()
        )
        if storage_object is None:
            raise _asset_storage_error(
                "ASSET_STORAGE_OBJECT_NOT_FOUND",
                "数据资产物化引用的对象存储记录不存在",
                record,
                storage_object_id=storage_object_id,
                status_code=404,
            )
        if (
            storage_object.tenant_id != record.tenant_id
            or storage_object.project_id != record.project_id
        ):
            raise _asset_storage_error(
                "ASSET_STORAGE_OBJECT_SCOPE_FORBIDDEN",
                "对象存储记录不属于当前租户和项目",
                record,
                storage_object_id=storage_object_id,
                status_code=403,
            )
        if (
            storage_object.source_type != record.run_type
            or storage_object.source_id != record.run_id
        ):
            raise _asset_storage_error(
                "ASSET_STORAGE_OBJECT_RUN_MISMATCH",
                "对象存储记录未绑定当前物化运行",
                record,
                storage_object_id=storage_object_id,
            )
        if storage_object.status not in ASSET_STORAGE_READY_STATUSES:
            raise _asset_storage_error(
                "ASSET_STORAGE_OBJECT_NOT_VERIFIED",
                "对象存储记录尚未通过上传和完整性验证",
                record,
                storage_object_id=storage_object_id,
                extra={"status": storage_object.status},
            )
        provider = str(storage_object.provider or "").strip().lower()
        if provider not in ASSET_STORAGE_PROVIDERS:
            raise _asset_storage_error(
                "ASSET_STORAGE_OBJECT_PROVIDER_INVALID",
                "数据资产物化只接受 MinIO、S3、OBS 或 OSS 对象",
                record,
                storage_object_id=storage_object_id,
                extra={"provider": provider},
            )
        object_key = str(storage_object.object_key or "").strip("/")
        missing_fields = [
            field
            for field, value in (
                ("bucket", storage_object.bucket),
                ("object_key", object_key),
                ("content_type", storage_object.content_type),
                ("object_key_sha256", storage_object.object_key_sha256),
            )
            if not str(value or "").strip()
        ]
        content_sha256 = str(storage_object.content_sha256 or "").strip().lower()
        if not SHA256_PATTERN.fullmatch(content_sha256):
            missing_fields.append("content_sha256")
        if storage_object.size_bytes is None or storage_object.size_bytes <= 0:
            missing_fields.append("size_bytes")
        if missing_fields:
            raise _asset_storage_error(
                "ASSET_STORAGE_OBJECT_METADATA_INCOMPLETE",
                "对象存储记录缺少可验证的内容元数据",
                record,
                storage_object_id=storage_object_id,
                extra={"missing_or_invalid_fields": missing_fields},
            )
        if not object_key.startswith(expected_prefix) or ".." in object_key.split("/"):
            raise _asset_storage_error(
                "ASSET_STORAGE_OBJECT_RUN_MISMATCH",
                "对象路径不在当前租户、项目和运行命名空间内",
                record,
                storage_object_id=storage_object_id,
            )
        expected_locator_hash = hashlib.sha256(object_key.encode("utf-8")).hexdigest()
        if storage_object.object_key_sha256.lower() != expected_locator_hash:
            raise _asset_storage_error(
                "ASSET_STORAGE_OBJECT_LOCATOR_INVALID",
                "对象路径哈希与登记记录不一致",
                record,
                storage_object_id=storage_object_id,
            )
        payload_run_id = storage_object.payload.get("run_id")
        if payload_run_id is not None and payload_run_id != record.run_id:
            raise _asset_storage_error(
                "ASSET_STORAGE_OBJECT_RUN_MISMATCH",
                "对象审计载荷未绑定当前物化运行",
                record,
                storage_object_id=storage_object_id,
            )
        if not storage_object.trace_id or storage_object.trace_id not in accepted_trace_ids:
            raise _asset_storage_error(
                "ASSET_STORAGE_OBJECT_TRACE_MISMATCH",
                "对象存储记录未绑定当前运行 Trace",
                record,
                storage_object_id=storage_object_id,
            )
        refs.append(
            {
                "kind": "storage_object",
                "storage_object_id": storage_object.storage_object_id,
                "provider": provider,
                "bucket": storage_object.bucket,
                "object_key": object_key,
                "content_type": storage_object.content_type,
                "size_bytes": storage_object.size_bytes,
                "content_sha256": content_sha256,
                "etag": storage_object.etag,
                "status": storage_object.status,
                "run_id": record.run_id,
            }
        )
    return refs


def materialization_payload(record: AssetMaterialization) -> dict[str, Any]:
    return {
        "materialization_id": record.materialization_id,
        "id": record.materialization_id,
        "status": record.status,
        "trace_id": record.trace_id,
        **record.payload,
    }


def lineage_edge_payload(record: AssetLineageEdge) -> dict[str, Any]:
    return {
        "edge_id": record.edge_id,
        "id": record.edge_id,
        "status": record.status,
        "trace_id": record.trace_id,
        **record.payload,
    }


def _asset_key_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.replace(",", "|").split("|") if item.strip()]
    if isinstance(value, list):
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return []


def normalize_asset_key_list(value: Any) -> list[str]:
    return _asset_key_list(value)


def _merged_observations(
    existing: dict[str, Any], asset_key: str, source: str
) -> list[dict[str, str]]:
    observations = existing.get("observations")
    items = observations if isinstance(observations, list) else []
    normalized = [
        item
        for item in items
        if isinstance(item, dict)
        and isinstance(item.get("asset_key"), str)
        and isinstance(item.get("lineage_source"), str)
    ]
    marker = {"asset_key": asset_key, "lineage_source": source}
    if marker not in normalized:
        normalized.append(marker)
    return normalized


def partition_payload(record: AssetPartition) -> dict[str, Any]:
    return {
        "asset_partition_id": record.asset_partition_id,
        "id": record.asset_partition_id,
        "status": record.status,
        "trace_id": record.trace_id,
        **record.payload,
    }


def upsert_data_asset_projection(
    session: Session,
    ctx: RequestContext,
    asset: dict[str, Any],
    *,
    status: str | None = None,
    trace_id: str | None = None,
) -> DataAsset:
    asset_key = str(asset["asset_key"])
    row = session.get(DataAsset, data_asset_id(ctx, asset_key))
    payload = {"asset_key": asset_key, **asset}
    row_status = status or str(asset.get("status") or "draft")
    row_trace_id = trace_id or asset.get("trace_id") or ctx.trace_id
    if row is None:
        row = DataAsset(
            data_asset_id=data_asset_id(ctx, asset_key),
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            status=row_status,
            trace_id=row_trace_id,
            payload=payload,
        )
        session.add(row)
    else:
        row.status = row_status
        row.trace_id = row_trace_id
        row.payload = {**row.payload, **payload}
    return row


def upsert_asset_lineage_edges(
    session: Session,
    ctx: RequestContext,
    *,
    asset_key: str,
    upstream_asset_keys: list[str],
    downstream_asset_keys: list[str],
    trace_id: str,
    source: str,
    materialization_id: str | None = None,
    run_id: str | None = None,
    partition_key: str | None = None,
) -> list[AssetLineageEdge]:
    rows: list[AssetLineageEdge] = []
    now = datetime.now(UTC).isoformat()
    lineage_cache = session.info.setdefault("asset_lineage_edge_cache", {})
    edge_specs = [
        {
            "source_asset_key": upstream,
            "target_asset_key": asset_key,
            "direction": "upstream",
        }
        for upstream in upstream_asset_keys
        if upstream != asset_key
    ] + [
        {
            "source_asset_key": asset_key,
            "target_asset_key": downstream,
            "direction": "downstream",
        }
        for downstream in downstream_asset_keys
        if downstream != asset_key
    ]
    seen_pairs: set[tuple[str, str]] = set()
    for spec in edge_specs:
        pair = (spec["source_asset_key"], spec["target_asset_key"])
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        edge_id = lineage_edge_id_for(
            ctx,
            spec["source_asset_key"],
            spec["target_asset_key"],
            materialization_id=materialization_id,
        )
        row = lineage_cache.get(edge_id)
        if row is None:
            row = session.get(AssetLineageEdge, edge_id)
        payload = {
            **spec,
            "asset_key": asset_key,
            "materialization_id": materialization_id,
            "run_id": run_id,
            "partition_key": partition_key,
            "lineage_source": source,
            "observed_at": now,
        }
        if row is None:
            row = AssetLineageEdge(
                edge_id=edge_id,
                tenant_id=ctx.tenant_id,
                project_id=ctx.project_id,
                status="active",
                trace_id=trace_id,
                payload={
                    **payload,
                    "observations": _merged_observations({}, asset_key, source),
                },
            )
            session.add(row)
        else:
            existing_payload = dict(row.payload or {})
            row.status = "active"
            row.trace_id = trace_id or row.trace_id
            row.payload = {
                **existing_payload,
                **payload,
                "observations": _merged_observations(existing_payload, asset_key, source),
            }
        lineage_cache[edge_id] = row
        rows.append(row)
    return rows


def seed_asset_materialization_projection(
    session: Session,
    ctx: RequestContext,
    asset: dict[str, Any],
) -> None:
    upsert_data_asset_projection(session, ctx, asset, status=asset.get("status"))
    asset_key = str(asset["asset_key"])
    trace_id = str(asset.get("trace_id") or f"trace_{asset_key}")
    upsert_asset_lineage_edges(
        session,
        ctx,
        asset_key=asset_key,
        upstream_asset_keys=_asset_key_list(asset.get("upstream")),
        downstream_asset_keys=_asset_key_list(asset.get("downstream")),
        trace_id=trace_id,
        source="seed_fixture",
    )
    materialization_id = asset.get("latest_materialization_id")
    if not materialization_id:
        return
    partition_key = str(
        asset.get("latest_partition_key") or f"{ctx.tenant_id}/{ctx.project_id}/unpartitioned"
    )
    run_id = str(asset.get("latest_run_id") or "seed_materialization")
    trace_id = str(asset.get("trace_id") or f"trace_{materialization_id}")
    materialization_status = str(asset.get("latest_materialization_status") or "success")
    materialization = AssetMaterialization(
        materialization_id=str(materialization_id),
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        status=materialization_status,
        trace_id=trace_id,
        payload={
            "asset_key": asset_key,
            "partition_key": partition_key,
            "run_id": run_id,
            "record_count": asset.get("record_count"),
            "quality_score": asset.get("quality_score"),
            "source": "seed_fixture",
        },
    )
    session.merge(materialization)
    session.merge(
        AssetPartition(
            asset_partition_id=partition_id(ctx, asset_key, partition_key),
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            status=materialization_status,
            trace_id=trace_id,
            payload={
                "asset_key": asset_key,
                "partition_key": partition_key,
                "materialization_id": str(materialization_id),
                "run_id": run_id,
                "quality_score": asset.get("quality_score"),
            },
        )
    )


def list_asset_lineage_edges(
    session: Session,
    ctx: RequestContext,
    asset_key: str,
    *,
    limit: int = 200,
) -> list[dict[str, Any]]:
    stmt = (
        select(AssetLineageEdge)
        .where(
            AssetLineageEdge.tenant_id == ctx.tenant_id,
            AssetLineageEdge.project_id == ctx.project_id,
        )
        .order_by(AssetLineageEdge.updated_at.desc(), AssetLineageEdge.edge_id.desc())
        .limit(limit)
    )
    return [
        lineage_edge_payload(record)
        for record in session.scalars(stmt)
        if record.payload.get("source_asset_key") == asset_key
        or record.payload.get("target_asset_key") == asset_key
    ]


def list_asset_materializations(
    session: Session,
    ctx: RequestContext,
    asset_key: str,
    *,
    limit: int | None,
) -> list[dict[str, Any]]:
    stmt = (
        select(AssetMaterialization)
        .where(
            AssetMaterialization.tenant_id == ctx.tenant_id,
            AssetMaterialization.project_id == ctx.project_id,
            AssetMaterialization.payload["asset_key"].as_string() == asset_key,
        )
        .order_by(
            AssetMaterialization.payload["materialized_at"].as_string().desc(),
            AssetMaterialization.updated_at.desc(),
            AssetMaterialization.materialization_id.desc(),
        )
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return [materialization_payload(record) for record in session.scalars(stmt)]


def list_asset_partitions(
    session: Session,
    ctx: RequestContext,
    asset_key: str,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    stmt = (
        select(AssetPartition)
        .where(
            AssetPartition.tenant_id == ctx.tenant_id,
            AssetPartition.project_id == ctx.project_id,
            AssetPartition.payload["asset_key"].as_string() == asset_key,
        )
        .order_by(
            AssetPartition.payload["updated_at"].as_string().desc(),
            AssetPartition.updated_at.desc(),
            AssetPartition.asset_partition_id.desc(),
        )
        .limit(limit)
    )
    return [partition_payload(record) for record in session.scalars(stmt)]


def _hotword_backfill_completion_target(
    session: Session,
    ctx: RequestContext,
    record: RunRecord,
    result_ref: dict[str, Any],
    impact_scope: dict[str, Any],
) -> tuple[str, str, str]:
    """Restore a governed backfill target exclusively from the frozen run."""

    asset_key = str(
        record.payload.get("asset_key") or record.payload.get("target_asset_key") or ""
    ).strip()
    if not asset_key:
        raise ApiError(
            "HOTWORD_BACKFILL_TARGET_ASSET_MISSING",
            "ASR 热词受控回填运行缺少冻结的目标资产",
            409,
            details=[{"run_id": record.run_id}],
        )
    if "asset_key" in result_ref and str(result_ref.get("asset_key") or "") != asset_key:
        raise ApiError(
            "HOTWORD_BACKFILL_TARGET_ASSET_MISMATCH",
            "完成回执不能改变 ASR 热词受控回填的目标资产",
            409,
            details=[
                {
                    "expected_asset_key": asset_key,
                    "actual_asset_key": result_ref.get("asset_key"),
                }
            ],
        )

    partition_key = str(
        record.partition_key or record.payload.get("partition_key") or "unpartitioned"
    ).strip()
    if (
        "partition_key" in result_ref
        and str(result_ref.get("partition_key") or "") != partition_key
    ):
        raise ApiError(
            "HOTWORD_BACKFILL_TARGET_PARTITION_MISMATCH",
            "完成回执不能改变 ASR 热词受控回填的目标分区",
            409,
            details=[
                {
                    "expected_partition_key": partition_key,
                    "actual_partition_key": result_ref.get("partition_key"),
                }
            ],
        )

    source_materialization_id = str(
        impact_scope.get("source_materialization_id")
        or impact_scope.get("materialization_id")
        or ""
    ).strip()
    if "materialization_id" in result_ref:
        supplied_id = str(result_ref.get("materialization_id") or "").strip()
        if supplied_id and supplied_id == source_materialization_id:
            raise ApiError(
                "HOTWORD_BACKFILL_TARGET_IS_SOURCE",
                "ASR 热词受控回填禁止覆盖原始物化记录",
                409,
                details=[{"source_materialization_id": source_materialization_id}],
            )
        if supplied_id and session.get(AssetMaterialization, supplied_id) is not None:
            raise ApiError(
                "HOTWORD_BACKFILL_TARGET_ALREADY_EXISTS",
                "ASR 热词受控回填目标物化记录已存在",
                409,
                details=[{"materialization_id": supplied_id}],
            )
        raise ApiError(
            "HOTWORD_BACKFILL_TARGET_ID_FORBIDDEN",
            "ASR 热词受控回填的目标物化 ID 由服务端生成，完成回执不得指定",
            409,
        )

    materialization_id = materialization_id_for(
        ctx,
        asset_key,
        partition_key,
        record.run_id,
    )
    if materialization_id == source_materialization_id:
        raise ApiError(
            "HOTWORD_BACKFILL_TARGET_IS_SOURCE",
            "ASR 热词受控回填禁止覆盖原始物化记录",
            409,
            details=[{"source_materialization_id": source_materialization_id}],
        )
    if session.get(AssetMaterialization, materialization_id) is not None:
        raise ApiError(
            "HOTWORD_BACKFILL_TARGET_ALREADY_EXISTS",
            "ASR 热词受控回填目标物化记录已存在",
            409,
            details=[{"materialization_id": materialization_id}],
        )
    return asset_key, partition_key, materialization_id


def materialize_asset_completion(
    session: Session,
    ctx: RequestContext,
    record: RunRecord,
    completion_receipt: dict[str, Any],
) -> list[dict[str, Any]]:
    result_ref = completion_receipt.get("result_ref")
    if not isinstance(result_ref, dict):
        result_ref = {}
    raw_impact_scope = record.payload.get("impact_scope")
    impact_scope = raw_impact_scope if isinstance(raw_impact_scope, dict) else {}
    if impact_scope.get("hotword_pack_version_id"):
        asset_key, partition_key, materialization_id = _hotword_backfill_completion_target(
            session,
            ctx,
            record,
            result_ref,
            impact_scope,
        )
    else:
        asset_key = str(
            result_ref.get("asset_key")
            or record.payload.get("asset_key")
            or record.payload.get("target_asset_key")
            or ""
        )
        if not asset_key:
            return []
        partition_key = str(
            result_ref.get("partition_key")
            or record.partition_key
            or record.payload.get("partition_key")
            or "unpartitioned"
        )
        materialization_id = str(
            result_ref.get("materialization_id")
            or materialization_id_for(ctx, asset_key, partition_key, record.run_id)
        )
    trace_id = record.trace_id
    now = datetime.now(UTC).isoformat()
    # Lock and verify every referenced object before any success row or projection is written.
    storage_refs = _verified_materialization_storage_refs(session, ctx, record, result_ref)
    checks = result_ref.get("checks") or record.payload.get("checks") or []
    upstream_asset_keys = _asset_key_list(
        result_ref.get("upstream_asset_keys")
        or result_ref.get("upstream")
        or record.payload.get("upstream_asset_keys")
        or record.payload.get("upstream")
    )
    downstream_asset_keys = _asset_key_list(
        result_ref.get("downstream_asset_keys")
        or result_ref.get("downstream")
        or record.payload.get("downstream_asset_keys")
        or record.payload.get("downstream")
    )
    hotword_governance = (
        {
            "hotword_pack_version_id": impact_scope.get("hotword_pack_version_id"),
            "eval_run_id": impact_scope.get("eval_run_id"),
            "task_version_id": impact_scope.get("task_version_id"),
            "source_materialization_id": impact_scope.get("source_materialization_id")
            or impact_scope.get("materialization_id"),
            "source_materialization_trace_id": impact_scope.get("source_materialization_trace_id"),
            "root_trace_id": impact_scope.get("root_trace_id") or trace_id,
            "overwrite_history": impact_scope.get("overwrite_history"),
        }
        if impact_scope.get("hotword_pack_version_id")
        else {}
    )
    hotword_governance = {
        key: value for key, value in hotword_governance.items() if value is not None
    }
    if not upstream_asset_keys or not downstream_asset_keys:
        asset_projection = session.scalar(
            select(JsonResource).where(
                JsonResource.collection == "data_assets",
                JsonResource.resource_key == asset_key,
                JsonResource.tenant_id == record.tenant_id,
                JsonResource.project_id == record.project_id,
            )
        )
        if asset_projection is not None:
            if not upstream_asset_keys:
                upstream_asset_keys = _asset_key_list(asset_projection.data.get("upstream"))
            if not downstream_asset_keys:
                downstream_asset_keys = _asset_key_list(asset_projection.data.get("downstream"))
    row_payload = {
        "asset_key": asset_key,
        "partition_key": partition_key,
        "run_id": record.run_id,
        "run_type": record.run_type,
        "materialization_id": materialization_id,
        "storage_refs": storage_refs,
        "checks": checks,
        "upstream_asset_keys": upstream_asset_keys,
        "downstream_asset_keys": downstream_asset_keys,
        "record_count": result_ref.get("record_count")
        or completion_receipt.get("metrics", {}).get("record_count"),
        "error_count": result_ref.get("error_count")
        or completion_receipt.get("metrics", {}).get("error_count"),
        "completion_receipt_id": completion_receipt.get("completion_receipt_id"),
        "completion_trace_id": completion_receipt.get("trace_id"),
        "materialized_at": now,
        **hotword_governance,
    }
    materialization = AssetMaterialization(
        materialization_id=materialization_id,
        tenant_id=record.tenant_id,
        project_id=record.project_id,
        status="success",
        trace_id=trace_id,
        payload=row_payload,
    )
    session.merge(materialization)
    lineage_rows = upsert_asset_lineage_edges(
        session,
        ctx,
        asset_key=asset_key,
        upstream_asset_keys=upstream_asset_keys,
        downstream_asset_keys=downstream_asset_keys,
        trace_id=trace_id,
        source="materialization_completion",
        materialization_id=materialization_id,
        run_id=record.run_id,
        partition_key=partition_key,
    )
    session.merge(
        AssetPartition(
            asset_partition_id=partition_id(ctx, asset_key, partition_key),
            tenant_id=record.tenant_id,
            project_id=record.project_id,
            status="success",
            trace_id=trace_id,
            payload={
                "asset_key": asset_key,
                "partition_key": partition_key,
                "run_id": record.run_id,
                "materialization_id": materialization_id,
                "updated_from_run_type": record.run_type,
                "updated_at": now,
            },
        )
    )
    asset_projection = session.scalar(
        select(JsonResource).where(
            JsonResource.collection == "data_assets",
            JsonResource.resource_key == asset_key,
            JsonResource.tenant_id == record.tenant_id,
            JsonResource.project_id == record.project_id,
        )
    )
    if asset_projection is not None:
        asset_projection.status = "success"
        asset_projection.trace_id = trace_id
        asset_projection.data = {
            **asset_projection.data,
            "status": "success",
            "latest_materialization_id": materialization_id,
            "latest_run_id": record.run_id,
            "latest_partition_key": partition_key,
            "latest_lineage_edge_ids": [row.edge_id for row in lineage_rows],
            "latest_hotword_governance": hotword_governance or None,
            "freshness": "刚刚更新",
        }
        upsert_data_asset_projection(
            session,
            ctx,
            asset_projection.data,
            status="success",
            trace_id=trace_id,
        )
    else:
        upsert_data_asset_projection(
            session,
            ctx,
            {
                "asset_key": asset_key,
                "display_name": asset_key.split("/")[-1],
                "status": "success",
                "latest_materialization_id": materialization_id,
                "latest_run_id": record.run_id,
                "latest_partition_key": partition_key,
                "latest_lineage_edge_ids": [row.edge_id for row in lineage_rows],
                "latest_hotword_governance": hotword_governance or None,
                "freshness": "刚刚更新",
            },
            status="success",
            trace_id=trace_id,
        )
    return [row_payload]
