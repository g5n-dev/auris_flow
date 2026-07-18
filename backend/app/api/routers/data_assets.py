from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import select

from app.api.deps import ContextDep, PaginationDep, SessionDep
from app.core.errors import ApiError
from app.core.rbac import require_any_role
from app.core.response import collection_envelope, envelope
from app.models import Badcase, HotwordVersionItem
from app.services.data_asset_materialization_service import (
    list_asset_lineage_edges,
    list_asset_materializations,
    list_asset_partitions,
    normalize_asset_key_list,
)
from app.services.hotword_service import validate_hotword_backfill_binding
from app.services.resource_service import (
    decode_asset_key,
    get_resource,
    list_resource_page,
    page_limit,
    status_counts,
)
from app.services.run_service import create_run

router = APIRouter(tags=["data-assets"])


def asset_with_trace(asset: dict, trace_id: str) -> dict:
    return {"trace_id": asset.get("trace_id", trace_id), **asset}


@router.get("/data-assets/recent")
def get_data_assets_recent(session: SessionDep, ctx: ContextDep, page: PaginationDep):
    resource_page = list_resource_page(session, ctx, "data_assets", page)
    items = [asset_with_trace(item, ctx.trace_id) for item in resource_page.items]
    return collection_envelope(
        items,
        ctx,
        total=resource_page.total,
        limit=resource_page.limit,
        next_cursor=resource_page.next_cursor,
        meta={"status_counts": status_counts(items)},
    )


@router.get("/data-assets")
def get_data_assets(
    session: SessionDep, ctx: ContextDep, page: PaginationDep, asset_key: str | None = None
):
    resource_page = list_resource_page(session, ctx, "data_assets", page)
    items = [asset_with_trace(item, ctx.trace_id) for item in resource_page.items]
    if asset_key:
        decoded = decode_asset_key(asset_key)
        items = [item for item in items if item.get("asset_key") == decoded]
    return collection_envelope(
        items,
        ctx,
        total=resource_page.total,
        limit=resource_page.limit,
        next_cursor=resource_page.next_cursor,
        meta={"status_counts": status_counts(items)},
    )


@router.get("/data-assets/{asset_key:path}/partitions")
def get_data_asset_partitions(
    asset_key: str, session: SessionDep, ctx: ContextDep, page: PaginationDep
):
    decoded = decode_asset_key(asset_key)
    items = list_asset_partitions(session, ctx, decoded, limit=page_limit(page))
    return collection_envelope(
        items,
        ctx,
        limit=page_limit(page),
        meta={"status_counts": status_counts(items)},
    )


@router.get("/data-assets/{asset_key:path}/materializations")
def get_data_asset_materializations(
    asset_key: str, session: SessionDep, ctx: ContextDep, page: PaginationDep
):
    decoded = decode_asset_key(asset_key)
    items = list_asset_materializations(session, ctx, decoded, limit=page_limit(page))
    return collection_envelope(
        items,
        ctx,
        limit=page_limit(page),
        meta={"status_counts": status_counts(items)},
    )


@router.get("/data-assets/{asset_key:path}/lineage")
def get_data_asset_lineage(asset_key: str, session: SessionDep, ctx: ContextDep):
    decoded = decode_asset_key(asset_key)
    resource = get_resource(session, ctx, "data_assets", decoded)
    data = resource.data
    dynamic_edges = list_asset_lineage_edges(session, ctx, decoded, limit=200)
    static_edges = [
        {
            "edge_id": f"static:{key}->{data['asset_key']}",
            "from": key,
            "to": data["asset_key"],
            "source_asset_key": key,
            "target_asset_key": data["asset_key"],
            "direction": "upstream",
            "lineage_source": "data_asset_projection",
            "status": "active",
            "trace_id": resource.trace_id or ctx.trace_id,
        }
        for key in normalize_asset_key_list(data.get("upstream"))
    ] + [
        {
            "edge_id": f"static:{data['asset_key']}->{key}",
            "from": data["asset_key"],
            "to": key,
            "source_asset_key": data["asset_key"],
            "target_asset_key": key,
            "direction": "downstream",
            "lineage_source": "data_asset_projection",
            "status": "active",
            "trace_id": resource.trace_id or ctx.trace_id,
        }
        for key in normalize_asset_key_list(data.get("downstream"))
    ]
    edge_by_pair: dict[tuple[str, str, str | None], dict] = {}
    for edge in static_edges + dynamic_edges:
        source = edge.get("source_asset_key") or edge.get("from")
        target = edge.get("target_asset_key") or edge.get("to")
        materialization_id = edge.get("materialization_id")
        if not isinstance(source, str) or not isinstance(target, str):
            continue
        current_direction = (
            "upstream"
            if target == data["asset_key"]
            else "downstream"
            if source == data["asset_key"]
            else edge.get("direction")
        )
        edge_by_pair[
            (source, target, materialization_id if isinstance(materialization_id, str) else None)
        ] = {
            **edge,
            "from": source,
            "to": target,
            "direction": current_direction,
        }
    edges = list(edge_by_pair.values())
    node_by_key: dict[str, dict] = {
        data["asset_key"]: {
            "asset_key": data["asset_key"],
            "direction": "current",
            "label": data.get("display_name") or data["asset_key"].split("/")[-1],
            "node_type": "asset",
            "trace_id": resource.trace_id or ctx.trace_id,
        }
    }
    for edge in edges:
        for endpoint in (edge["from"], edge["to"]):
            if endpoint in node_by_key:
                continue
            node_by_key[endpoint] = {
                "asset_key": endpoint,
                "direction": "upstream" if edge["to"] == data["asset_key"] else "downstream",
                "label": endpoint.split("/")[-1],
                "node_type": "asset",
                "trace_id": edge.get("trace_id") or ctx.trace_id,
            }
    # A lineage response is a traceability surface, not a recent-activity card.
    # Load the complete scoped materialization history so older governance chains
    # do not disappear after the sixth backfill.
    materializations = list_asset_materializations(session, ctx, decoded, limit=None)
    for materialization in materializations:
        materialization_id = materialization.get("materialization_id")
        if not isinstance(materialization_id, str):
            continue
        node_by_key[materialization_id] = {
            "asset_key": materialization_id,
            "direction": "runtime",
            "label": materialization_id,
            "node_type": "materialization",
            "run_id": materialization.get("run_id"),
            "partition_key": materialization.get("partition_key"),
            "trace_id": materialization.get("trace_id") or ctx.trace_id,
        }
        edges.append(
            {
                "edge_id": f"runtime:{materialization_id}->{data['asset_key']}",
                "from": materialization_id,
                "to": data["asset_key"],
                "source_asset_key": materialization_id,
                "target_asset_key": data["asset_key"],
                "direction": "materialized",
                "lineage_source": "asset_materialization",
                "materialization_id": materialization_id,
                "run_id": materialization.get("run_id"),
                "partition_key": materialization.get("partition_key"),
                "trace_id": materialization.get("trace_id") or ctx.trace_id,
            }
        )

    governed_materializations = [
        materialization
        for materialization in materializations
        if isinstance(materialization.get("hotword_pack_version_id"), str)
    ]
    governed_version_ids = {
        str(materialization["hotword_pack_version_id"])
        for materialization in governed_materializations
    }
    source_badcases_by_version: dict[str, set[str]] = {
        version_id: set() for version_id in governed_version_ids
    }
    if governed_version_ids:
        source_items = session.scalars(
            select(HotwordVersionItem).where(
                HotwordVersionItem.tenant_id == ctx.tenant_id,
                HotwordVersionItem.project_id == ctx.project_id,
                HotwordVersionItem.version_id.in_(governed_version_ids),
                HotwordVersionItem.source_badcase_id.is_not(None),
            )
        ).all()
        for item in source_items:
            if item.source_badcase_id:
                source_badcases_by_version.setdefault(item.version_id, set()).add(
                    item.source_badcase_id
                )
    governed_badcase_ids = {
        badcase_id
        for badcase_ids in source_badcases_by_version.values()
        for badcase_id in badcase_ids
    }
    badcase_lineage_by_id: dict[str, dict[str, str | None]] = {}
    if governed_badcase_ids:
        governed_badcases = session.scalars(
            select(Badcase).where(
                Badcase.tenant_id == ctx.tenant_id,
                Badcase.project_id == ctx.project_id,
                Badcase.badcase_id.in_(governed_badcase_ids),
            )
        ).all()
        badcase_lineage_by_id = {
            badcase.badcase_id: {
                "trace_id": badcase.root_trace_id or badcase.trace_id or ctx.trace_id,
                "evidence_storage_object_id": badcase.evidence_storage_object_id,
            }
            for badcase in governed_badcases
        }

    governance_edge_ids: set[str] = set()

    def append_governance_node(
        node_id: str,
        *,
        node_type: str,
        label: str,
        trace_id: str,
    ) -> None:
        existing = node_by_key.get(node_id)
        if existing is not None:
            existing["root_trace_id"] = trace_id
            governance_roles = existing.get("governance_roles")
            roles = list(governance_roles) if isinstance(governance_roles, list) else []
            if node_type not in roles:
                roles.append(node_type)
            existing["governance_roles"] = roles
            return
        node_by_key[node_id] = {
            "asset_key": node_id,
            "direction": "governance",
            "label": label,
            "node_type": node_type,
            "trace_id": trace_id,
            "root_trace_id": trace_id,
            "governance_roles": [node_type],
        }

    def append_governance_edge(
        source: str,
        target: str,
        *,
        relation: str,
        trace_id: str,
        materialization_id: str,
    ) -> None:
        edge_id = f"hotword:{relation}:{source}->{target}:{materialization_id}"
        if edge_id in governance_edge_ids:
            return
        governance_edge_ids.add(edge_id)
        edges.append(
            {
                "edge_id": edge_id,
                "from": source,
                "to": target,
                "source_asset_key": source,
                "target_asset_key": target,
                "direction": "governance",
                "lineage_source": "hotword_governance",
                "relation": relation,
                "materialization_id": materialization_id,
                "trace_id": trace_id,
            }
        )

    for materialization in governed_materializations:
        materialization_id = str(materialization["materialization_id"])
        version_id = str(materialization["hotword_pack_version_id"])
        eval_run_id = str(materialization.get("eval_run_id") or "")
        task_version_id = str(materialization.get("task_version_id") or "")
        source_materialization_id = str(materialization.get("source_materialization_id") or "")
        backfill_run_id = str(materialization.get("run_id") or "")
        root_trace_id = str(materialization.get("root_trace_id") or ctx.trace_id)

        append_governance_node(
            version_id,
            node_type="hotword_pack_version",
            label=f"热词版本 {version_id}",
            trace_id=root_trace_id,
        )
        if source_materialization_id:
            append_governance_node(
                source_materialization_id,
                node_type="source_materialization",
                label=f"原 ASR 物化 {source_materialization_id}",
                trace_id=str(
                    materialization.get("source_materialization_trace_id") or root_trace_id
                ),
            )
        badcase_ids = sorted(source_badcases_by_version.get(version_id, set()))
        for badcase_id in badcase_ids:
            badcase_lineage = badcase_lineage_by_id.get(badcase_id, {})
            badcase_trace_id = str(badcase_lineage.get("trace_id") or root_trace_id)
            evidence_id = str(
                badcase_lineage.get("evidence_storage_object_id")
                or f"hotword-evidence:{badcase_id}"
            )
            append_governance_node(
                evidence_id,
                node_type="evidence",
                label=f"词级证据 {badcase_id}",
                trace_id=badcase_trace_id,
            )
            append_governance_node(
                badcase_id,
                node_type="badcase",
                label=f"ASR Badcase {badcase_id}",
                trace_id=badcase_trace_id,
            )
            append_governance_edge(
                evidence_id,
                badcase_id,
                relation="supports",
                trace_id=root_trace_id,
                materialization_id=materialization_id,
            )
            append_governance_edge(
                badcase_id,
                version_id,
                relation="fixed-by",
                trace_id=root_trace_id,
                materialization_id=materialization_id,
            )
        if source_materialization_id and not badcase_ids:
            append_governance_edge(
                source_materialization_id,
                version_id,
                relation="candidate-source",
                trace_id=root_trace_id,
                materialization_id=materialization_id,
            )
        prior_node_id = version_id
        for node_id, node_type, label, relation in (
            (eval_run_id, "eval_run", "热词影子 EvalRun", "evaluated-by"),
            (task_version_id, "task_version", "生产 TaskVersion", "bound-to"),
            (backfill_run_id, "backfill_run", "受控回填 Run", "executed-by"),
            (materialization_id, "materialization", "新 ASR 物化", "materialized-as"),
        ):
            if not node_id:
                continue
            append_governance_node(
                node_id,
                node_type=node_type,
                label=f"{label} {node_id}",
                trace_id=root_trace_id,
            )
            append_governance_edge(
                prior_node_id,
                node_id,
                relation=relation,
                trace_id=root_trace_id,
                materialization_id=materialization_id,
            )
            prior_node_id = node_id
        if source_materialization_id and backfill_run_id:
            append_governance_edge(
                source_materialization_id,
                backfill_run_id,
                relation="reprocessed-by",
                trace_id=root_trace_id,
                materialization_id=materialization_id,
            )
    return envelope(
        {
            "asset": asset_with_trace(data, resource.trace_id or ctx.trace_id),
            "nodes": list(node_by_key.values()),
            "edges": edges,
            "materializations": materializations,
        },
        ctx,
    )


@router.post("/data-assets/{asset_key:path}/backfills", status_code=202)
async def post_data_asset_backfills(
    asset_key: str, request: Request, session: SessionDep, ctx: ContextDep
):
    require_any_role(ctx, ("project_admin", "asset_manager"), "data_assets.backfill")
    body = await request.json()
    if not isinstance(body, dict):
        raise ApiError("BACKFILL_REQUEST_INVALID", "回填请求必须是 JSON 对象", 422)
    decoded = decode_asset_key(asset_key)
    get_resource(session, ctx, "data_assets", decoded)
    impact_scope = body.get("impact_scope")
    affected_objects: list[dict[str, str]] = [{"type": "data_asset", "id": decoded}]
    run_trace_id: str | None = None
    if isinstance(impact_scope, dict) and impact_scope.get("hotword_pack_version_id"):
        normalized_scope, affected_objects = validate_hotword_backfill_binding(
            session,
            ctx,
            asset_key=decoded,
            impact_scope=impact_scope,
        )
        body = {
            **body,
            "impact_scope": normalized_scope,
            "root_trace_id": normalized_scope["root_trace_id"],
        }
        # 热词回填是治理根链上的受控运行；其他通用回填仍使用请求 Trace。
        run_trace_id = normalized_scope["root_trace_id"]
    return await create_run(
        session,
        ctx,
        request,
        run_type="asset_backfill",
        event_type="backfill.requested",
        payload={
            **body,
            "asset_key": decoded,
            "affected_objects": affected_objects,
        },
        status="pending",
        run_trace_id=run_trace_id,
    )


@router.post("/data-assets/{asset_key:path}/checks/retry", status_code=202)
async def post_data_asset_checks_retry(
    asset_key: str, request: Request, session: SessionDep, ctx: ContextDep
):
    body = await request.json()
    decoded = decode_asset_key(asset_key)
    return await create_run(
        session,
        ctx,
        request,
        run_type="asset_check_retry",
        event_type="asset_check.retry_requested",
        payload={**body, "asset_key": decoded},
        status="pending",
    )


@router.get("/data-assets/{asset_key:path}")
def get_data_assets_by_asset_key(asset_key: str, session: SessionDep, ctx: ContextDep):
    resource = get_resource(session, ctx, "data_assets", asset_key)
    return envelope(asset_with_trace(resource.data, resource.trace_id or ctx.trace_id), ctx)
